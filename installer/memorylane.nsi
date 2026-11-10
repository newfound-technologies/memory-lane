; Memory Lane installer. Build:  makensis -DVERSION=1.12.0 memorylane.nsi
Unicode true
!ifndef VERSION
  !define VERSION "0.0.0"
!endif
!include "MUI2.nsh"
!include "LogicLib.nsh"

Name "Memory Lane"
OutFile "Memory Lane Setup.exe"
InstallDir "$LOCALAPPDATA\Memory Lane"
RequestExecutionLevel user
SetCompressor /SOLID lzma
BrandingText "Memory Lane ${VERSION}"
ShowInstDetails show
VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "Memory Lane"
VIAddVersionKey "FileDescription" "Memory Lane family photo library"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" "Private family software"

!define MUI_ICON "stage\app\memorylane.ico"
!define MUI_UNICON "stage\app\memorylane.ico"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "Welcome to Memory Lane"
!define MUI_WELCOMEPAGE_TEXT "Memory Lane gathers your family photos into one clean, private library on your computer.$\r$\n$\r$\nThis will set everything up. It needs an internet connection for a few minutes the first time, and it will download anything that is missing.$\r$\n$\r$\nClick Next to continue."
!define MUI_COMPONENTSPAGE_TEXT_TOP "Choose where you want Memory Lane's icon."
!define MUI_FINISHPAGE_TITLE "Memory Lane is ready"
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Open Memory Lane now"
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchApp

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Function LaunchApp
  Exec '"$INSTDIR\venv\Scripts\pythonw.exe" "$INSTDIR\app\photokeeper.py" --app'
FunctionEnd

Section "Memory Lane (required)" SecMain
  SectionIn RO
  SetOutPath "$INSTDIR\app"
  File /r "stage\app\*.*"
  SetOutPath "$INSTDIR"
  File "setup-env.ps1"

  DetailPrint "Setting up Memory Lane. The first time this can take several minutes."
  nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\setup-env.ps1" -InstallDir "$INSTDIR"'
  Pop $0
  ${If} $0 != 0
    MessageBox MB_ICONSTOP|MB_OK "Memory Lane could not finish setting up (code $0).$\r$\n$\r$\nMake sure this computer is online, then run this Setup again. The details are in the list on the previous screen."
    Abort
  ${EndIf}

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\MemoryLane" "DisplayName" "Memory Lane"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\MemoryLane" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\MemoryLane" "Publisher" "Memory Lane"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\MemoryLane" "DisplayIcon" "$INSTDIR\app\memorylane.ico"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\MemoryLane" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\MemoryLane" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\MemoryLane" "NoRepair" 1
SectionEnd

Section "Icon on the desktop" SecDesktop
  SetOutPath "$INSTDIR\app"
  CreateShortcut "$DESKTOP\Memory Lane.lnk" "$INSTDIR\venv\Scripts\pythonw.exe" '"$INSTDIR\app\photokeeper.py" --app' "$INSTDIR\app\memorylane.ico" 0 SW_SHOWNORMAL "" "Memory Lane - your family photo library"
SectionEnd

Section "Start menu entry" SecStart
  SetOutPath "$INSTDIR\app"
  CreateShortcut "$SMPROGRAMS\Memory Lane.lnk" "$INSTDIR\venv\Scripts\pythonw.exe" '"$INSTDIR\app\photokeeper.py" --app' "$INSTDIR\app\memorylane.ico" 0 SW_SHOWNORMAL "" "Memory Lane - your family photo library"
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMain} "The program and everything it needs. Your photos are never changed."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop} "Puts the Memory Lane icon on your desktop."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecStart} "Adds Memory Lane to the Windows Start menu."
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Section "Uninstall"
  nsExec::Exec 'schtasks /Delete /TN "PhotoKeeperWeekly" /F'
  Delete "$DESKTOP\Memory Lane.lnk"
  Delete "$SMPROGRAMS\Memory Lane.lnk"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\MemoryLane"
  MessageBox MB_ICONINFORMATION|MB_OK "Memory Lane was removed.$\r$\n$\r$\nYour photos and your library folder were not touched."
SectionEnd
