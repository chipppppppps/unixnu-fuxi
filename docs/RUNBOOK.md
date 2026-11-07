# 小米 13（fuxi）UniXNU 启动镜像 — 复现手册

> 状态：2026-11-07。一条命令可以从改完代码跑到手机上出现 panic 屏。
> 已打通到 **第 7 环（用户态）**：`boot_v4.img` 上机后 launchd 作为 PID 1 活着，
> restored_external / mount_tmpfs 一起跑，没有 panic。看不到界面是因为没有显示驱动。
>
> 这不是能日常用的 iOS。原因见 §1.2，剩下的墙都不是调参能绕过去的。

---

## 1. 这套东西是什么

### 1.1 是什么

UniXNU 是一个把 XNU（苹果内核）搬到非苹果 ARM64 设备上跑的项目。这条线是
`OddlyLabs/UniXNU@fuxi` + `OddlyLabs/uefi-fuxi@fuxi` + 板级仓库 `unixnu-fuxi`，
目标设备小米 13（代号 fuxi，SM8550），目标用户态 iPadOS 17.7.11（21H461）。

启动链六层：Android boot 镜像 → UEFI（Silicium）→ UniXNU 启动菜单 →
DarwinBoot（XNU 加载器）→ 内核入口 → 存储/根卷 → 用户态。

镜像是 `fastboot boot` **临时启动**的，不写任何分区。仓库 AGENTS.md 的原话：
"Never flash the device. A test image is started with `fastboot boot` only."

### 1.2 能到哪、不能到哪

| 关 | 事实 | 后果 |
| --- | --- | --- |
| 存储 | 内核里 UFS 驱动为 0，根卷是打进 payload 的 **137 MB 内存盘**（APFS，恢复盘） | 恢复盘的设计目的是「一次性装机」，不是长期运行的系统。要真系统必须有 UFS 驱动 |
| 信任链 | `RestoreTrustCache` 的模块是 **version 1**，DarwinBoot 与内核只认 **version 2** 容器 | 已解决：`scripts/pack-restore-trust.py` 把它逐条重序列化，见 §4 |
| 显示 | Display / GPU 在 fuxi 这条线上仍是 `planned` | 用户态在跑也没有界面，只有 UEFI framebuffer 画的文本 |

当前能拿到的终点（**2026-11-07 已实测到达**）：三色带 → 内核起来 → APFS 根卷挂上 →
`launchd` 作为 PID 1 活下来 → `restored_external`、`mount_tmpfs` 一起跑 → **没有 panic**，
屏幕停在色带，因为没人画界面。

镜像没有 USB 栈，屏幕是唯一的信息来源；没有任何 panic 文字时，点一下屏幕可以让触控探针
在抬手时 panic 并打印 `SYS` 行，列出活着的进程 —— 这是当前唯一的内省手段（见 §6）。

---

## 2. 一次性准备

### 2.1 本机工具

```powershell
winget install --id Python.Python.3.12 -e --scope user
winget install --id Git.Git -e
winget install --id GitHub.cli -e --scope user
winget install --id Google.PlatformTools -e
python -m pip install --proxy http://127.0.0.1:65533 pyyaml   # 只在改 build.yml 后校验用
```

`openssl` 用 Git 自带的那份（`C:\Program Files\Git\usr\bin\openssl.exe`），不要另装。

### 2.2 目录布局

```
unixnu-fuxi\                 <- $root
├─ UniXNU\                   <- git clone OddlyLabs/UniXNU，checkout fuxi
├─ uefi-fuxi\                <- git clone OddlyLabs/uefi-fuxi，checkout fuxi
├─ unixnu-fuxi\              <- git clone 你的 fork（板级仓库）
├─ keys\                     <- 自己的 RSA 密钥对
├─ tools\chunked_download.py <- 分块下载器（代理会把长连接掐断）
├─ rebuild.ps1               <- 一条命令
├─ decrypt.cmd               <- 解密 CI 产物
└─ RUNBOOK.md / RESULTS-*.md
```

`UniXNU` 的 20 个 submodule 要 `--init --depth 1`；`uefi-fuxi` 的嵌套 openssl
子模块在 Windows 上会报 fatal，不影响使用。

### 2.3 fork 板级仓库并换公钥（必须做）

CI 的最后一个动作是用 `ci/boot-image-recipient.pem` 加密 boot.img，私钥只在机主手上。
不换成自己的公钥，自己 fork 跑出来的产物解不开。

```powershell
# 生成 RSA-4096 密钥对
& "C:\Program Files\Git\usr\bin\openssl.exe" genrsa -out keys\fuxi-boot-private.pem 4096
& "C:\Program Files\Git\usr\bin\openssl.exe" rsa -in keys\fuxi-boot-private.pem `
    -pubout -out keys\fuxi-boot-recipient.pem

# 提交到 fork
$pem = (Get-Content keys\fuxi-boot-recipient.pem -Raw).Trim() + "`n"
$sha = (gh api repos/<you>/unixnu-fuxi/contents/ci/boot-image-recipient.pem | ConvertFrom-Json).sha
$body = @{ message = 'Point the boot image at my own recipient key'
           content = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pem))
           branch  = 'main'; sha = $sha } | ConvertTo-Json
$body | Out-File -Encoding ascii $env:TEMP\pem.json
gh api --method PUT repos/<you>/unixnu-fuxi/contents/ci/boot-image-recipient.pem --input $env:TEMP\pem.json
```

### 2.4 gh 必须带 `workflow` scope

没有它，`PUT contents` 写 `.github/workflows/*` 会返回 **404**（GitHub 用 404 伪装权限不足，
不是 403，很容易被误判成路径写错），于是你改的 yml 永远上不去。

`gh auth refresh -s workflow` 在非交互终端会**挂死**。可行的做法是自己走一次 device flow：

```powershell
$p = @{ Proxy = 'http://127.0.0.1:65533' }
$d = Invoke-RestMethod -Method Post -Uri 'https://github.com/login/device/code' @p `
       -Headers @{ Accept = 'application/json' } `
       -Body @{ client_id = '178c6fc778ccc68e1d6a'; scope = 'repo read:org gist workflow' }
# 打开 $d.verification_uri 输入 $d.user_code，然后轮询：
while ($true) {
  $t = Invoke-RestMethod -Method Post -Uri 'https://github.com/login/oauth/access_token' @p `
         -Headers @{ Accept = 'application/json' } `
         -Body @{ client_id = '178c6fc778ccc68e1d6a'; device_code = $d.device_code
                  grant_type = 'urn:ietf:params:oauth:grant-type:device_code' }
  if ($t.access_token) { $t.access_token | Out-File -Encoding ascii $env:TEMP\gh_new_token.txt; break }
  if ($t.error -notin 'authorization_pending', 'slow_down') { throw $t.error }
  Start-Sleep $d.interval
}
```

装进 keyring 时，当前环境里已有的 `GH_TOKEN` 会让 `gh auth login --with-token` 直接失败，
要先清掉：`cmd.exe /c "set GH_TOKEN=&& gh auth login --with-token < %TEMP%\gh_new_token.txt"`。
装完 `gh auth status` 里应该能看到 `workflow`。

### 2.5 手机

- 解锁 BL（`fastboot getvar unlocked` 应为 `yes`；本机设备序列号 `9165fcf`）。
- 进 fastboot：关机 → 音量下 + 电源 → 插线 → `fastboot devices`。

---

## 3. 我们相对上游改了什么

四个脚本 + 两个配置，全部在 fork `chipppppppps/unixnu-fuxi` 上。

| 文件 | 改动 | 为什么 |
| --- | --- | --- |
| `board.toml` | `[payload] kernel_args` 末尾加 `rd=md0-ramblock` | 既让 `bsd_init` 跳过密封根校验（`bsd_rooted_ramdisk()` 只看 `rd` 前缀是不是 `md0`），又因为长度不是 3 字符让 `IOFindBSDRoot` 继续按卷 UUID 匹配 |
| `board.toml` | `trust_cache_sha256 = ["7416ca4f…"]` | 白名单锁定两模块 bundle 的哈希（见 §4） |
| `board.toml` | `[payload.adt] root_match_volume_uuid = "9071148B-B025-402D-AD7A-48464D8B5FFF"` | 让根卷按 UUID 找到恢复盘那个 APFS 卷 |
| `board.toml` | `[payload.adt] packed_data_ramdisk = true` | ADT 里生成 `PackedRAMDisk` 节点，DarwinBoot 才会走压缩盘分支 |
| `.github/workflows/build.yml` | 新增 `Fetch the restore ramdisk and the trust bundle` | `ipsw fetch` 三个组件（RestoreRamDisk / StaticTrustCache / Cryptex1,SystemTrustCache），解 IM4P、打 DBRDISK1、打两模块 trust bundle，落 `$RUNNER_TEMP` |
| `.github/workflows/build.yml` | `Build payload` 加 `--trust-cache` `--data-ramdisk` | 把上面两样塞进 payload 卷 |
| `scripts/unpack-restore-ramdisk.py` | 新文件 | 剥掉 IM4P 外壳（DER 里 `rdsk` → OCTET STRING），得到 176 MiB 裸 APFS 盘 |
| `scripts/pack-data-ramdisk.py` | 新文件 | 打成 DarwinBoot 的 `DBRDISK1`（只留非零页，137 MB），并复刻 `PackedRamdisk.h` 的全部合法性检查，避免这边过了手机上失败 |
| `scripts/pack-trust-bundle.py` | 新文件 | 两个 version 2 模块拼成 `count + offsets[] + modules` 容器（用于 OS 卷；**恢复盘不能用它**，见 §4） |
| `scripts/pack-restore-trust.py` | 新文件 | 把恢复盘那份 version 1 信任缓存逐条重序列化成 version 2（267 条，产物 6,440 B）—— 这才是让 launchd 活下来的那份 |

关键认知：**fuxi 的 `bootimg --ramdisk` 参数不是 ramdisk**。`carrier == 'image'` 时它就是
payload 卷本身（`bootimg.py:114-127`），所以恢复盘只能走 `--data-ramdisk` → payload 卷里的
`Payloads/scratch-data.img` → DarwinBoot 的 `PackedRAMDisk` 这条路。

---

## 4. 最值钱的一课：信任链只认 version 2

### 现象

屏幕停在 UniXNU 菜单，一个选项列表，下面一行小字，**一条色带都没有**（连绿条都没有）。

### 机制

- 菜单停住并且出现小字 = 启动条目已经试过并失败。
  `Main.c:160-167`：条目返回后 `UnicodeSPrint (Ui.Notice, …, L"'%s' returned: %r", …)`，
  同时 `Countdown = FALSE`，所以菜单不再倒计时。
- 绿条在 `DarwinBoot.c:811` 才画，也就是**所有检查之后**。没绿条 = 死在 handoff 之前。
- 真正的检查在 `DarwinBoot.c:371-400 ValidTrustCaches`：它要的是容器格式
  `u32 count` + `u32 offsets[count]` + 各模块，且**每个模块起始 u32 必须等于 2**，
  entry 数在 `Start+20`，`End - Start == 24 + entries * 24`。

### 坑

`RestoreTrustCache`（`Firmware/044-75545-038.dmg.trustcache`，5,921 B）是 IM4P 包着的
**version 1** 模块（`16 04 "rtsc"` / `16 01 "1"` / 内层 `01 00 00 00`）。
version 1 过不了 `version == 2` 的检查 → `DarwinBoot.c:675` 返回 `EFI_COMPROMISED_DATA`
→ 回菜单 + 小字。看起来像「菜单卡死」，其实是启动失败。

### 先用过的那份：static + system bundle（**对恢复盘是错的**）

两模块 bundle，构造式与 `UniXNU/scripts/build-original-system-trust.py:53` 一致：

```
bundle = u32(2) + u32(12) + u32(12 + len(static)) + static + system
```

- `static` = `StaticTrustCache`（`Firmware/044-76040-038.dmg.trustcache`，59,519 B → 模块 59,496 B / 2478 entries）
- `system` = `Cryptex1,SystemTrustCache`（`Firmware/044-76034-038.dmg.trustcache`，1,871 B → 模块 1,848 B / 76 entries）

产物 **61,356 B**，sha256 `7416ca4f8377146626bb284e2133c76bee6cb4e3ee0af5d648b186235a7323a7`
—— 与 `UniXNU/profiles/os/ipados-17.7.11-21H461/profile.toml` 里 `[ipsw.sha256] trust_cache_sha256`
的第一项逐字节相同。拼对了确实会对上，但**它信任的是 OS / SystemOS 卷里的二进制**。

用它跑出来的结果：DarwinBoot 放行了（绿条出现）、根卷挂上了、`/sbin/launchd` exec 成功了，
然后 PID 1 立刻死，屏幕首行 `initproc failed to start` —— 因为它自己的 cdhash 不在信任表里，
AMFI 在 exec 之后把它杀掉。**哈希对得上 ≠ 信任的对像是对的。**

### 正解：把恢复盘自己那份转成 version 2

原始 `RestoreTrustCache` 是 v1：`version=1`、`uuid=cfc8a0d5…`、`count=267`、
每条 22 字节（cdhash 20 + flags 2），body 5,898 B。v2 的头部与它同构（24 B），
只是每条 24 字节，所以要**逐条重新序列化**（`scripts/pack-restore-trust.py`）：

```
bundle = u32(1) + u32(8) + [u32(2) + uuid + u32(267) + [cdhash(20) + u32(flags)] × 267]
       = 6,440 B   sha256 750b5d8ac5f5b52b8fd02d261caf96dd318db0904b3651ca05d9ae4939de488a
```

脚本按 `ValidTrustCaches` 的全部规则自检（count ≤ 128、offset 对齐、模块 version == 2、
`End - Start == 24 + entries*24`、flags ∈ 1..4、cdhash 严格升序），cdhash 一个都不改。

**实测结果**：换上它之后（run 42356995919，`boot_v4.img`）屏上不再有 panic 文字，
点一下屏幕让探针 panic，`SYS` 行是
`1:launchd 3:restored_esterna 4:mount_tmpfs n3` —— PID 1 活下来了。
所以「信任链」这一关的结论不是「无解」，而是**信任的对像必须是恢复盘自己的 cdhash**。

`evidence/2026-11-02-first-boot.md:541-543` 记的 2026-11-03 r3-ramblock 那次
（launchd / restored_external / mount_tmpfs 三个进程活着）走的就是这条路。

### 顺带排除过的嫌疑

`packed_data_ramdisk` 不是原因。用 `PackedRamdisk.h` 的 `RamdiskLayout` 规则把
`restore-data.dbr`（137,445,888 B，30 extent，`cursor == inputsize`）逐条验算过，通过。
initrd 读取路径（`Boot.c:146-151` → `UniXNUReadFile`）也不是原因，它和 `kernel.macho`
同卷同目录，v1 镜像已经证明可读。

---

## 5. 跑一遍

```powershell
cd $root

.\rebuild.ps1                          # 派 CI → 等 → 下 → 解 → 验
.\rebuild.ps1 -Flash                   # 再 fastboot boot 到手机上
.\rebuild.ps1 -RunId 42349310071       # 复用已完成的 run，不重新派
.\rebuild.ps1 -NoBuild -ArtifactDir _artifact_v3 -Image boot_x.img   # 只解密 + 校验
```

脚本做六件事：dispatch → 每 20 s 打印当前 step → 取 `fuxi-boot-image-encrypted-*` →
`tools\chunked_download.py` 分块下载并核对字节数 → `decrypt.cmd` → `ci\check-boot-image.py`
→（可选）`fastboot boot`。

手工等价命令：

```powershell
$env:HTTPS_PROXY = 'http://127.0.0.1:65533'
gh api --method POST repos/<you>/unixnu-fuxi/actions/workflows/410089205/dispatches -f ref=main
# 等 run 完成，然后
python tools\chunked_download.py `
  "https://api.github.com/repos/<you>/unixnu-fuxi/actions/artifacts/<id>/zip" _a.zip `
  --chunk 4194304 --retries 15 `
  --header "Authorization: token $(gh auth token)" --header "Accept: application/vnd.github+json"
Expand-Archive _a.zip _a
cmd /c "decrypt.cmd `"$PWD\_a`" `"$PWD\boot.img`""
cd unixnu-fuxi
python ci\check-boot-image.py ..\boot.img ..\_a\boot.json ..\uefi-fuxi
```

注意：fork 上**第一次** push `.github/workflows/` 不会触发 workflow（workflow 记录是随这次
push 才注册的，事件求值时还不存在），所以要手动 dispatch。

---

## 6. 上机与观色带

```powershell
fastboot -s 9165fcf devices
fastboot -s 9165fcf boot boot_v3.img
```

| 颜色 | 含义 |
| --- | --- |
| 绿 | DarwinBoot 交出控制权（`DarwinBoot.c:811` 之后） |
| 蓝 | 进内核 |
| 品红 | 即将开 MMU |

三色带全出 → 内核在跑；随后出文字 → panic 屏。**上机后不要点屏幕**：
`fuxi-touch-hid=1` 时探针只在「HID 启动稳定之后（最多 20 秒）」的那次抬手才结束，
点早了不算，反而会让探针抢在等根设备的超时之前 panic。

---

## 7. 失败隔离表

| 屏幕 | 含义 | 下一步 |
| --- | --- | --- |
| 全黑，无任何输出 | UEFI 没起来 | 换 `--target RELEASE`，或用 `entry_probe_reset_*` 探针判断卡点 |
| 只有固件日志二维码 | UniXNU.efi 没加载 | 检查 payload 在 FAT 卷里的路径 |
| 菜单 + 底部一行小字 `'UniXNU' returned: …` | 启动条目失败并回菜单 | 那个 `%r` 就是错误码；`EFI_COMPROMISED_DATA` → §4 信任链 |
| 菜单 + 一直不倒计时 | 同上（Notice 非空会抑制倒计时） | 同上 |
| 绿条后立刻自动重启 | DarwinBoot 前段崩溃 | `entry_probe_reset_delay` / `entry_probe_reset_after` |
| 蓝条后长时间无字 | 内核早期（MMU/中断）卡住 | 检查 `sheng-smp` / `armcpu-spi-ipi` 组合；fuxi 上 SGI 收不到，必须走 SPI |
| `Failed to mount root device @IOKitBSDInit.cpp:984` | 没有可挂的根卷，通常是**没点屏幕**导致触控探针没走完 | 重刷并连续点 40 秒以上，或去掉 `fuxi-touch-hid=1` |
| 首行 `initproc failed to start` | 根卷挂上了、launchd 也 exec 了，但 PID 1 在生出第一个子进程前退出 | **几乎都是信任链喂错**（用了 static+system bundle 而不是恢复盘自己的 cdhash），见 §4 |
| 首行 `CS_KILLED initproc failed to start` | AMFI 校验失败杀进程 | 同上，信任链或代码签名问题 |
| 出现 `FTS pwr …` / `FTS rd…` 两行 | 触控探针抢先 panic（`fuxi-touch-panic=2`） | 探针数据已拿到，正常；想继续启动就把屏幕丢开别碰 |
| 色带上去之后**没有任何文字**，也不重启 | 没有 panic，内核与用户态都在跑 | **这是最好的一种**：点一下屏幕，让探针 panic 打出 `SYS` 行，看活着几个进程 |

---

## 8. 时间与额度

| job | 耗时 |
| --- | --- |
| `Host tests (Linux)` | ~2 分钟 |
| `Kernel-target compile (Xcode clang)` | ~2 分钟 |
| `fuxi boot image (…)` | ~7 分钟，其中 `Build kernel` 约 6 分 40 秒 |
| **一次完整 CI** | **约 11 分钟** |

`Fetch the restore ramdisk and the trust cache` 约 13 秒（走 HTTP range 只取需要的字节，
不下 6.8 GB 的 IPSW）；`Build payload` 约 5 秒。最近一次实测：16:48:10Z 起跑 →
16:59:47Z 收尾，**11 分 37 秒**。

payload 卷硬上限：`size_mib = 192`，`payload.py:230-233` 卡在 95% = 182.4 MiB。
当前（v4）占用约 189.5 MB（kernel 52,088,832 + dbr 137,445,888 + adt 14,484 +
trustcache **6,440** + toml 541），余量约 1.7 MiB。再加文件就得先抬 `size_mib`，
注意 boot.img 整体还要过 ABL。

---

## 9. 产物与校验

能跑到用户态的那张：`boot_v4.img` **144,691,200 B**，
sha256 `9cdfb91f6330b7afdc8eb483a9db7c24b61daa6826aeb617b8474c58d09c783d`
（run 42356995919，板级 `612c30e`）。上一版 `boot_v3.img` 144,744,448 B，
sha256 `e1e0388e…` 会停在 `initproc failed to start`，别拿错。

`ci/check-boot-image.py` 会逐条打印 ok，值得看懂的是这几条：

- `image size 0xc300200 = shim 0x200 + firmware 0x300000 + payload 0xc000000`
  —— 结构没变，只是 payload 卷变大了。
- `payload FAT32 volume 'FUXIESP', 192 MiB` 里应该有五项：
  `kernel.macho` / `kernel.macho.adt` / `kernel.macho.trustcache` / `scratch-data.img` / `unixnu.toml`。
  **`scratch-data.img` 就是恢复盘**，没有它第 6 环无从谈起；
  **`kernel.macho.trustcache` 应该是 6,440 B**（恢复盘的转换产物），61,356 B 那份会让 PID 1 死。
- `boot menu entry xnu:` 那一行末尾应有 `rd=md0-ramblock`。

---

## 10. 已知坑清单

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `gh run download` 卡死，目标目录都不建 | 大产物 + 代理 | 用 `tools\chunked_download.py` |
| `Invoke-WebRequest` 下到 92–95 秒断，报 `从传输流收到意外的 EOF 或 0 个字节` | 代理掐长连接 | 同上（4 MiB 一块，每块独立重试） |
| 分块下载第一块就 403 `Server failed to authenticate…` | API 302 到已签名的 Azure blob，`Authorization` 被一起带过去 | 先截断 302 拿 `Location`，之后只对签名 URL 发无鉴权请求（v2 已实现） |
| `mv: cannot create regular file '/b/t_v3.img'` | `decrypt.cmd` 用相对名，`%~2` 第 4 字符起切 | 已改为 `set "OUT=%~f2"` |
| `PUT contents` 对 `.github/workflows/*` 返回 **404** | token 缺 `workflow` scope | §2.4 |
| 新文件 `GET contents` 也返回 **404** | 文件本来就不存在 | 把 404 当「新建、不带 sha」 |
| fork 第一次 push 不触发 workflow | workflow 记录随这次 push 才注册 | 手动 `dispatches` |
| `if` 当表达式报「无法将 if 项识别为 cmdlet」、`??` 报语法错 | DSH 的 pwsh 实际是 Windows PowerShell 5.1 | 用 `if` 语句 / `Join-Path` |
| `.ps1` 里的中文变乱码 | PS 5.1 按 ANSI 读无 BOM 的 .ps1 | 脚本保持 ASCII |
| `unixnu ipsw info/fetch` 抛 `FileNotFoundError: [WinError 2]` | 它要调 `git` | 把 `C:\Program Files\Git\cmd` 加进 `PATH` |
| `unixnu tree` 报 `OSError: [WinError 1314] 客户端没有所需的特权` | 要建符号链接 | Windows 原生走不到这一步，直接用 CI |

---

## 11. 现状与下一步

里程碑：

| # | 关卡 | 状态 |
| --- | --- | --- |
| 1 | UEFI 固件 | ✅ |
| 2 | UniXNU 菜单 | ✅ |
| 3 | payload 卷 | ✅ |
| 4 | DarwinBoot handoff（绿条） | ✅ 2026-11-07 |
| 5 | 内核入口 / MMU / 7 个副核 | ✅（`SMP … run7`） |
| 6 | 挂载根卷（APFS 内存盘） | ✅ 2026-11-07 首次 |
| 7 | 用户态 | ✅ 2026-11-07（`boot_v4.img`，见下） |

第 7 环的实测证据（`boot_v4.img`）：屏幕不出 panic 文字，点一下屏幕让探针 panic 后

```
FTS …                       （两行探针数据）
SYS root apfs /dev/disk1s1 1:launchd 3:restored_esterna 4:mount_tmpfs n3
```

`/dev/disk1s1` 是 NativeRamBlock 的内存盘挂成的 APFS 根卷，PID 1 是 launchd、
pid 3 是 restored_external、pid 4 是 mount_tmpfs，`n3` = 三个进程。
与 2026-11-03 的 r3-ramblock 记录逐字一致。**没有界面**不代表内核没跑起来 ——
是这条线没有 Display/GPU 驱动，没人画。

已经做完的两条路：

- **A. 收尾（完成）** — 这份文档 + `rebuild.ps1` + `tools\chunked_download.py`
  + `RESULTS-2026-11-07.md`：一条命令从 CI 跑到加密镜像，再到校验。
- **B. 再推一次第 7 环（完成，成了）** — `scripts/pack-restore-trust.py` 把恢复盘那份
  version 1 信任缓存重序列化成 version 2（267 条，bundle 6,440 B，sha256 `750b5d8a…`），
  `build.yml` 与 `board.toml` 跟着改，run 42356995919 构建出 `boot_v4.img`，上机即见上表。
  这条同时验证了 §4 的推断：**信任的对像错了，哈希对得上也没用**。

还剩一条没动的路：

- **C. 换底座（未做，可评估）** — 社区另一条线（`yigezhong011-ops/ios-fuxi`）已经到桌面：
  它把参考 superproject 换成 `research/UniXNU@fb6aad6`、目标内核
  **xnu-10063.141.1 / Darwin 23**，用上百个补丁把 NativeUfs / NativeAdreno /
  NativeMobileDisplay 逐个移植，还写了 USB 调试通道。我们这条是 xnu-8796，
  补丁不兼容，等于换项目。

如果继续沿本线走（不换底座），挡在前面的其实就是两条：**UFS 驱动**（把根卷从 137 MB
内存盘换成真 userdata 上的 APFS）与**显示/GPU 驱动**（有东西能画）。这两件事都不是调参，
是写驱动。
