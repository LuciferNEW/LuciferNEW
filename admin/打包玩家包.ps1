# 打包玩家包.ps1 — 把客端 + 玩家啟動器 壓成一個 zip,排除所有伺服器檔
param(
  [string]$ClientRoot = "D:\sf",
  [string]$PlayerPkg  = "D:\sf2_project\player_package",
  [string]$Out        = "D:\sf2_project\player_package\SF2_玩家包.zip"
)
$ErrorActionPreference = "Stop"
chcp 65001 > $null
Add-Type -AssemblyName System.IO.Compression.FileSystem

# 伺服器/私有檔:絕不打包
$excludeName = @('SFOffline.exe','SFOffline.patched.exe','save.db','local-lobby.sqlite3')
$excludeTop  = @('serverdata')          # 客端根目錄下要整個排除的資料夾
$excludeExt  = @('.bak','.log','.pdb')

Write-Host "掃描客端檔案: $ClientRoot ..."
$root = (Resolve-Path $ClientRoot).Path.TrimEnd('\')
$files = Get-ChildItem -LiteralPath $root -Recurse -File | Where-Object {
  $rel = $_.FullName.Substring($root.Length).TrimStart('\')
  $top = ($rel -split '\\')[0]
  -not (($excludeName -contains $_.Name) -or ($excludeTop -contains $top) -or ($excludeExt -contains $_.Extension.ToLower()))
}
Write-Host ("要打包 {0} 個客端檔" -f $files.Count)

if (Test-Path $Out) { Remove-Item $Out -Force }
$zip = [System.IO.Compression.ZipFile]::Open($Out,'Create')
try {
  $i = 0
  foreach ($f in $files) {
    $rel = $f.FullName.Substring($root.Length).TrimStart('\')   # 放 zip 根(啟動器要在 Binaries 同層)
    [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip,$f.FullName,$rel,[System.IO.Compression.CompressionLevel]::Fastest) | Out-Null
    $i++; if ($i % 200 -eq 0) { Write-Host ("  {0}/{1}" -f $i,$files.Count) }
  }
  # 玩家啟動器放 zip 根(和 Binaries 同層,bat 的 %~dp0Binaries 才找得到)
  if (Test-Path $PlayerPkg) {
    foreach ($pf in Get-ChildItem -LiteralPath $PlayerPkg -File) {
      [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip,$pf.FullName,$pf.Name) | Out-Null
    }
  }
} finally { $zip.Dispose() }

$size = [math]::Round((Get-Item $Out).Length/1MB,1)
Write-Host ""
Write-Host ("完成 -> {0}  ({1} MB)" -f $Out,$size) -ForegroundColor Green
Write-Host "這個 zip 只有客端 + 啟動器,沒有任何伺服器檔。直接丟給朋友即可。"
