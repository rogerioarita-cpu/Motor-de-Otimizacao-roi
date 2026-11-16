Unicode true
!include "MUI2.nsh"

!define NOME "Motor de Otimização e Retenção com ROI"
!define NOME_CURTO "Motor ROI (C&C)"
!define VERSAO "1.0.0"
!define CHAVE "Software\Microsoft\Windows\CurrentVersion\Uninstall\MotorROI_CC"

Name "${NOME}"
Caption "Instalador do ${NOME}"
OutFile "Instalar_Motor_ROI_CC.exe"
InstallDir "$LOCALAPPDATA\Programs\Motor ROI CC"
InstallDirRegKey HKCU "${CHAVE}" "InstallLocation"
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetCompressorDictSize 64
BrandingText "C&C - ${NOME} ${VERSAO}"

!define MUI_ICON "motor\cc.ico"
!define MUI_UNICON "motor\cc.ico"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_FUNCTION AbrirPrograma
!define MUI_FINISHPAGE_RUN_TEXT "Abrir o ${NOME_CURTO} agora"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "PortugueseBR"

Function AbrirPrograma
  SetOutPath "$INSTDIR\motor"
  Exec '"$INSTDIR\motor\python\pythonw.exe" "$INSTDIR\motor\motor_roi.pyw"'
FunctionEnd

Section "Programa"
  SectionIn RO
  ; instalação anterior: remove o programa antes de copiar a nova versão
  RMDir /r "$INSTDIR\motor"
  SetOutPath "$INSTDIR"
  File /r "motor"
  WriteUninstaller "$INSTDIR\Desinstalar.exe"

  SetOutPath "$INSTDIR\motor"
  CreateShortcut "$SMPROGRAMS\${NOME_CURTO}.lnk" "$INSTDIR\motor\python\pythonw.exe" '"$INSTDIR\motor\motor_roi.pyw"' "$INSTDIR\motor\cc.ico"
  CreateShortcut "$DESKTOP\${NOME_CURTO}.lnk" "$INSTDIR\motor\python\pythonw.exe" '"$INSTDIR\motor\motor_roi.pyw"' "$INSTDIR\motor\cc.ico"

  WriteRegStr HKCU "${CHAVE}" "DisplayName" "${NOME}"
  WriteRegStr HKCU "${CHAVE}" "DisplayVersion" "${VERSAO}"
  WriteRegStr HKCU "${CHAVE}" "Publisher" "C&C"
  WriteRegStr HKCU "${CHAVE}" "DisplayIcon" "$INSTDIR\motor\cc.ico"
  WriteRegStr HKCU "${CHAVE}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${CHAVE}" "UninstallString" '"$INSTDIR\Desinstalar.exe"'
  WriteRegDWORD HKCU "${CHAVE}" "NoModify" 1
  WriteRegDWORD HKCU "${CHAVE}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  Delete "$SMPROGRAMS\${NOME_CURTO}.lnk"
  Delete "$DESKTOP\${NOME_CURTO}.lnk"
  RMDir /r "$INSTDIR\motor"
  Delete "$INSTDIR\Desinstalar.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${CHAVE}"
SectionEnd
