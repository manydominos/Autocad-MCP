@echo off
REM ==== McpPointCloud add-in: build ====
REM Builds addin\McpPointCloud\McpPointCloud.cs into
REM backends\addin\McpPointCloud.bundle\Contents\McpPointCloud.dll (committed, shipped in the wheel).
REM
REM Needs: Windows with .NET Framework 4.x (csc.exe in %WINDIR%\Microsoft.NET\Framework64\v4.0.30319)
REM        and, once, internet for the AutoCAD 2021 reference assemblies (NuGet AutoCAD.NET 24.0.0,
REM        Autodesk; reference only, never redistributed - kept outside the checkout in
REM        %LOCALAPPDATA%\autocad-mcp\addin-ref\24.0.0, never committed).
REM Usage: addin\McpPointCloud\build.cmd

setlocal
set HERE=%~dp0
set REF=%LOCALAPPDATA%\autocad-mcp\addin-ref\24.0.0
set OUT=%HERE%..\..\backends\addin\McpPointCloud.bundle\Contents
set CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe

if not exist "%CSC%" (echo NAPAKA: ni %CSC% & exit /b 1)
if not exist "%REF%\AcDbMgd.dll" (
  echo Prenasam sklice AutoCAD 2021 ^(NuGet AutoCAD.NET 24.0.0^) ...
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference='Stop'; $r='%REF%'; New-Item -ItemType Directory -Force $r | Out-Null;" ^
    "foreach($p in 'autocad.net.core','autocad.net.model','autocad.net'){" ^
    " $z=Join-Path $r ($p+'.zip'); Invoke-WebRequest -UseBasicParsing ('https://api.nuget.org/v3-flatcontainer/'+$p+'/24.0.0/'+$p+'.24.0.0.nupkg') -OutFile $z;" ^
    " $d=Join-Path $r $p; Expand-Archive -Force $z $d; Copy-Item (Join-Path $d 'lib\net47\*.dll') $r -Force }" || exit /b 1
)
if not exist "%OUT%" mkdir "%OUT%"
"%CSC%" -nologo -target:library -platform:x64 -optimize+ -out:"%OUT%\McpPointCloud.dll" ^
  -r:"%REF%\AcDbMgd.dll" -r:"%REF%\AcCoreMgd.dll" -r:"%REF%\AcMgd.dll" ^
  -r:System.Web.Extensions.dll -r:System.Core.dll ^
  "%HERE%McpPointCloud.cs" || exit /b 1
echo OK: %OUT%\McpPointCloud.dll
endlocal
