# 打包一个干净的源码分发包（含驱动安装程序），输出到桌面。
# 用法：在项目根目录运行  ->  powershell -ExecutionPolicy Bypass -File make_release.ps1

$ErrorActionPreference = 'Stop'
$root    = $PSScriptRoot
$stamp   = Get-Date -Format 'yyyyMMdd'
$relName = "D7_PMU_IAP_工具_发布包_$stamp"
$out     = Join-Path ([Environment]::GetFolderPath('Desktop')) $relName

# 驱动安装包的来源（如路径变了改这里）
$greenBase = Join-Path ([Environment]::GetFolderPath('Desktop')) '绿色软件'
$zcanpro   = Join-Path $greenBase 'CANFD分析仪资料20250213(固件V2.11以上)\调试工具\CAN(FD)-bus综合应用软件ZCanPro\ZCanPro\ZCANPRO_Setup_V2.2.5(20230203).exe'
$usbdrv    = Join-Path $greenBase 'CANFD分析仪资料20250213(固件V2.11以上)\硬件驱动程序\USB驱动安装工具Setup(V1.40).exe'

# 清掉旧的
if (Test-Path $out) { Remove-Item $out -Recurse -Force }
New-Item -ItemType Directory -Path $out | Out-Null

# --- 1) 拷源码（排除 .venv / 缓存 / git / 旧发布包等）---
$src = Join-Path $out '主程序'
New-Item -ItemType Directory -Path $src | Out-Null
$exclude = @('.venv', '.git', '.pytest_cache', '.qtcreator', '__pycache__', 'build', 'dist')
robocopy $root $src /E /XD $($exclude | ForEach-Object { Join-Path $root $_ }) /XF '*.pyc' 'make_release.ps1' | Out-Null
# robocopy 成功返回码 < 8，正常化
if ($LASTEXITCODE -ge 8) { throw "robocopy 失败，code=$LASTEXITCODE" }

# --- 2) 拷驱动安装包 ---
$drv = Join-Path $out '驱动'
New-Item -ItemType Directory -Path $drv | Out-Null
foreach ($f in @($zcanpro, $usbdrv)) {
    if (Test-Path $f) { Copy-Item $f $drv } else { Write-Warning "找不到驱动文件: $f" }
}

# --- 3) 顶层放一份「先看我.txt」---
@"
安装顺序：
1. 进入「驱动」文件夹，先装 ZCANPRO_Setup，再装 USB驱动安装工具，插上 CAN 盒子。
2. 进入「主程序」文件夹，按《源码运行说明.md》装 64 位 + 32 位 Python 并运行。
"@ | Out-File (Join-Path $out '先看我.txt') -Encoding utf8

# --- 4) 压缩 ---
$zip = "$out.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $out -DestinationPath $zip
Write-Host "完成：" -ForegroundColor Green
Write-Host "  文件夹: $out"
Write-Host "  压缩包: $zip"
