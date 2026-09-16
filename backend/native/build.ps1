# Build proc_loopback.exe with clang targeting the MSVC ABI. clang locates the Visual Studio toolchain and Windows SDK itself.
$ErrorActionPreference = "Stop"
$src = Join-Path $PSScriptRoot "proc_loopback.cpp"
$out = Join-Path $PSScriptRoot "proc_loopback.exe"
clang++ --target=x86_64-pc-windows-msvc -std=c++17 -O2 -DUNICODE -D_UNICODE -DWIN32_LEAN_AND_MEAN -fuse-ld=lld $src -o $out -lole32 -loleaut32 -lmmdevapi -lavrt -Xlinker /SUBSYSTEM:CONSOLE
if ($LASTEXITCODE -ne 0) { throw "build failed" }
Write-Host "built $out"
