Unicode true
!include "MUI2.nsh"

; Gerado pelo montar_instalador.ps1, que passa: /DVERSAO=x.y.z /DMOTOR=<pasta motor> /DSAIDA=<exe>
!ifndef VERSAO
  !define VERSAO "0.0.0"
!endif
!ifndef MOTOR
  !define MOTOR "..\build\roi\motor"
!endif
!ifndef SAIDA
  !define SAIDA "..\dist\Instalar_Motor_ROI_CC_${VERSAO}.exe"
!endif

!define NOME "Motor de Otimização e Retenção com ROI"
!define NOME_CURTO "Motor ROI (C&C)"
!define PROGRAMA "motor_roi.pyw"
!define CHAVE "Software\Microsoft\Windows\CurrentVersion\Uninstall\MotorROI_CC"

Name "${NOME}"
Caption "Instalador do ${NOME} ${VERSAO}"
OutFile "${SAIDA}"
InstallDir "$LOCALAPPDATA\Programs\Motor ROI CC"
InstallDirRegKey HKCU "${CHAVE}" "InstallLocation"
RequestExecutionLevel user
!ifdef RAPIDO
  SetCompressor zlib            ; empacotamento rápido (instalador maior)
!else
  SetCompressor /SOLID lzma
  SetCompressorDictSize 64
!endif
BrandingText "C&C - ${NOME} ${VERSAO}"

!define MUI_ICON "${MOTOR}\cc.ico"
!define MUI_UNICON "${MOTOR}\cc.ico"
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
  Exec '"$INSTDIR\motor\python\pythonw.exe" "$INSTDIR\motor\${PROGRAMA}"'
FunctionEnd

Section "Programa"
  SectionIn RO
  ; instalação anterior: remove o programa antes de copiar a nova versão
  RMDir /r "$INSTDIR\motor"
  SetOutPath "$INSTDIR\motor"
  File /r "${MOTOR}\*.*"
  SetOutPath "$INSTDIR"
  WriteUninstaller "$INSTDIR\Desinstalar.exe"

  SetOutPath "$INSTDIR\motor"
  CreateShortcut "$SMPROGRAMS\${NOME_CURTO}.lnk" "$INSTDIR\motor\python\pythonw.exe" '"$INSTDIR\motor\${PROGRAMA}"' "$INSTDIR\motor\cc.ico"
  CreateShortcut "$DESKTOP\${NOME_CURTO}.lnk" "$INSTDIR\motor\python\pythonw.exe" '"$INSTDIR\motor\${PROGRAMA}"' "$INSTDIR\motor\cc.ico"

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
