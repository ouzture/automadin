#if Ver < EncodeVer(6, 7, 0)
  #error Inno Setup 6.7 or newer is required for RedirectionGuard.
#endif

#define ProductionAppName "Automadin"
#define ProductionAppId "{{14E54D2D-A7E4-4519-859B-AB98812DC5B3}"

#ifndef MyAppVersion
  #error MyAppVersion must be supplied from pyproject.toml with /DMyAppVersion=x.y.z
#endif
#ifdef InstallerTestMode
  #ifndef MyAppName
    #error InstallerTestMode requires an isolated MyAppName.
  #endif
  #ifndef MyAppId
    #error InstallerTestMode requires an isolated MyAppId.
  #endif
  #ifndef InstallerTestDefaultDirName
    #error InstallerTestMode requires an explicit scratch InstallerTestDefaultDirName.
  #endif
  #if MyAppName == ProductionAppName
    #error InstallerTestMode cannot use the production application name.
  #endif
  #if MyAppId == ProductionAppId
    #error InstallerTestMode cannot use the production AppId.
  #endif
  #define MyAppDefaultDirName InstallerTestDefaultDirName
#else
  #ifdef MyAppName
    #if MyAppName != ProductionAppName
      #error Production installer application name cannot be overridden.
    #endif
  #else
    #define MyAppName ProductionAppName
  #endif
  #ifdef MyAppId
    #if MyAppId != ProductionAppId
      #error Production installer AppId cannot be overridden.
    #endif
  #else
    #define MyAppId ProductionAppId
  #endif
  #define MyAppDefaultDirName "{localappdata}\Programs\" + MyAppName
#endif
#ifndef MyAppPublisher
  #define MyAppPublisher "Automadin"
#endif
#define MyAppExeName "Automadin.exe"
#ifndef SourceDir
  #define SourceDir "..\dist\Automadin.dist"
#endif
#ifndef InstallerOutputDir
  #define InstallerOutputDir "..\dist\installer"
#endif
#ifndef InstallerOutputBaseFilename
  #define InstallerOutputBaseFilename "Automadin-Setup-" + MyAppVersion
#endif
#define MyInstallManifestSHA256 GetSHA256OfFile(SourceDir + "\install-files.manifest")

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={#MyAppDefaultDirName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir={#InstallerOutputDir}
OutputBaseFilename={#InstallerOutputBaseFilename}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
PrivilegesRequired=lowest
RedirectionGuard=yes
UsePreviousAppDir=yes
#ifdef InstallerTestMode
AppMutex=Local\Automadin.InstallerSmoke.{#MyAppId}
#else
AppMutex=Local\Automadin.Application.14E54D2D-A7E4-4519-859B-AB98812DC5B3
#endif
CloseApplications=yes
RestartApplications=no
; A successful upgrade owns only its current payload.  In particular, a user
; file recreated at an obsolete path after upgrade must survive uninstall.
UninstallLogMode=overwrite

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Excludes: "install-files.manifest"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\install-files.manifest"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\install-files.manifest"; Flags: dontcopy

#ifndef InstallerTestMode
[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Registry]
Root: HKCU; Subkey: "Software\Google\Chrome\NativeMessagingHosts\com.automadin.browser_companion"; ValueType: string; ValueName: ""; ValueData: "{app}\browser-companion-native-host.json"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Microsoft\Edge\NativeMessagingHosts\com.automadin.browser_companion"; ValueType: string; ValueName: ""; ValueData: "{app}\browser-companion-native-host.json"; Flags: uninsdeletekey

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Automadin"; Flags: nowait postinstall skipifsilent runasoriginaluser; Check: FileExists(ExpandConstant('{app}\{#MyAppExeName}'))
#endif

[Code]
const
  ManagedManifestHeader = 'AUTOMADIN-INSTALL-MANIFEST-V1';
  ManagedManifestName = 'install-files.manifest';
  MaxManagedFileCount = 200000;
  MaxManagedPathLength = 1024;
  FileAttributeReparsePoint = $400;
  TrustedIncomingManifestSHA256 = '{#MyInstallManifestSHA256}';

var
  CachedPreviousFiles: TArrayOfString;
  CachedPreviousIsValid: Boolean;
  CachedIncomingFiles: TArrayOfString;
  InstallerPrepared: Boolean;
  InstallerPreparationResult: String;

function GetFileAttributesW(lpFileName: String): Cardinal;
  external 'GetFileAttributesW@kernel32.dll stdcall';

function EndsWithText(const Value, Suffix: String): Boolean;
begin
  Result :=
    (Length(Value) >= Length(Suffix)) and
    (CompareText(Copy(Value, Length(Value) - Length(Suffix) + 1, Length(Suffix)), Suffix) = 0);
end;

function NormalizeManagedPath(const Value: String; var Normalized: String): Boolean;
var
  I: Integer;
  Padded: String;
begin
  Result := False;
  if (Value = '') or (Value <> Trim(Value)) or
     (Length(Value) > MaxManagedPathLength) then
    Exit;
  Normalized := Value;
  StringChangeEx(Normalized, '/', '\', True);
  if Normalized[1] = '\' then
    Exit;
  for I := 1 to Length(Normalized) do
    if (Ord(Normalized[I]) < 32) or (Ord(Normalized[I]) > 126) or
       (Pos(Normalized[I], ':*?<>|"') > 0) then
      Exit;
  Padded := '\' + Normalized + '\';
  if (Pos('\\', Normalized) > 0) or
     (Pos('\.\', Padded) > 0) or
     (Pos('\..\', Padded) > 0) or
     (Pos('.\', Normalized) > 0) or
     (Pos(' \', Normalized) > 0) or
     EndsWithText(Normalized, '.') or
     EndsWithText(Normalized, ' ') then
    Exit;
  Result := True;
end;

function IsCanonicalSHA256(const Value: String): Boolean;
var
  I: Integer;
begin
  Result := Length(Value) = 64;
  if not Result then
    Exit;
  for I := 1 to Length(Value) do
    if not (((Value[I] >= '0') and (Value[I] <= '9')) or
            ((Value[I] >= 'a') and (Value[I] <= 'f'))) then
    begin
      Result := False;
      Exit;
    end;
end;

function IsProtectedUserPath(const RelativePath: String): Boolean;
var
  LowerPath, Padded: String;
begin
  LowerPath := Lowercase(RelativePath);
  Padded := '\' + LowerPath + '\';
  Result :=
    (Pos('\data\', Padded) > 0) or
    (Pos('\profiles\', Padded) > 0) or
    (Pos('\runtime\', Padded) > 0) or
    (Pos('\logs\', Padded) > 0) or
    (LowerPath = 'portable.flag') or
    EndsWithText(LowerPath, '.db') or
    EndsWithText(LowerPath, '.db-journal') or
    EndsWithText(LowerPath, '.db-shm') or
    EndsWithText(LowerPath, '.db-wal') or
    EndsWithText(LowerPath, '.sqlite') or
    EndsWithText(LowerPath, '.sqlite-journal') or
    EndsWithText(LowerPath, '.sqlite-shm') or
    EndsWithText(LowerPath, '.sqlite-wal') or
    EndsWithText(LowerPath, '.sqlite3') or
    EndsWithText(LowerPath, '.sqlite3-journal') or
    EndsWithText(LowerPath, '.sqlite3-shm') or
    EndsWithText(LowerPath, '.sqlite3-wal') or
    ((Pos('unins', LowerPath) = 1) and
      (EndsWithText(LowerPath, '.exe') or
       EndsWithText(LowerPath, '.dat') or
       EndsWithText(LowerPath, '.msg')));
end;

function CompareManagedPathsOrdinal(const Left, Right: String): Integer;
var
  I, SharedLength: Integer;
  LowerLeft, LowerRight: String;
begin
  LowerLeft := Lowercase(Left);
  LowerRight := Lowercase(Right);
  SharedLength := Length(LowerLeft);
  if Length(LowerRight) < SharedLength then
    SharedLength := Length(LowerRight);
  for I := 1 to SharedLength do
  begin
    if Ord(LowerLeft[I]) < Ord(LowerRight[I]) then
    begin
      Result := -1;
      Exit;
    end;
    if Ord(LowerLeft[I]) > Ord(LowerRight[I]) then
    begin
      Result := 1;
      Exit;
    end;
  end;
  if Length(LowerLeft) < Length(LowerRight) then
    Result := -1
  else if Length(LowerLeft) > Length(LowerRight) then
    Result := 1
  else
    Result := 0;
end;
function IsValidManagedManifest(
  var Lines: TArrayOfString;
  const Description: String
): Boolean;
var
  I: Integer;
  Normalized, Previous: String;
begin
  Result := False;
  if (GetArrayLength(Lines) < 2) or
     (GetArrayLength(Lines) > MaxManagedFileCount + 1) or
     (Lines[0] <> ManagedManifestHeader) then
  begin
    Log(Description + ' has an invalid header or entry count; cleanup is disabled.');
    Exit;
  end;
  Previous := '';
  for I := 1 to GetArrayLength(Lines) - 1 do
  begin
    if not NormalizeManagedPath(Lines[I], Normalized) then
    begin
      Log(Description + ' contains an unsafe path; cleanup is disabled: ' + Lines[I]);
      Exit;
    end;
    if IsProtectedUserPath(Normalized) then
    begin
      Log(Description + ' contains a protected user path; cleanup is disabled: ' + Normalized);
      Exit;
    end;
    if (Previous <> '') and
       (CompareManagedPathsOrdinal(Previous, Normalized) >= 0) then
    begin
      Log(Description + ' paths are duplicated or not strictly sorted: ' + Normalized);
      Exit;
    end;
    Previous := Normalized;
  end;
  Result := True;
end;

function PathContainsReparsePoint(const RelativePath: String): Boolean;
var
  Attributes: Cardinal;
  Separator: Integer;
  Current, Remaining, Component: String;
begin
  Result := True;
  Current := RemoveBackslashUnlessRoot(ExpandConstant('{app}'));
  Attributes := GetFileAttributesW(Current);
  if (Attributes <> $FFFFFFFF) and
     ((Attributes and FileAttributeReparsePoint) <> 0) then
  begin
    Log('Managed cleanup refused because the application root is a reparse point: ' + Current);
    Exit;
  end;

  Remaining := RelativePath;
  while True do
  begin
    Separator := Pos('\', Remaining);
    if Separator = 0 then
      Break;
    Component := Copy(Remaining, 1, Separator - 1);
    Current := AddBackslash(Current) + Component;
    Attributes := GetFileAttributesW(Current);
    if (Attributes <> $FFFFFFFF) and
       ((Attributes and FileAttributeReparsePoint) <> 0) then
    begin
      Log('Managed cleanup refused through a reparse-point parent: ' + Current);
      Exit;
    end;
    Delete(Remaining, 1, Separator);
  end;
  Result := False;
end;

function IsPathShapeConflict(
  const PreviousPath, IncomingPath: String
): Boolean;
begin
  Result :=
    (Pos(Lowercase(PreviousPath) + '/', Lowercase(IncomingPath)) = 1) or
    (Pos(Lowercase(IncomingPath) + '/', Lowercase(PreviousPath)) = 1);
end;

function FindPathShapeConflict(
  var PreviousFiles, IncomingFiles: TArrayOfString;
  var Description: String
): Boolean;
var
  PreviousIndex, LowIndex, HighIndex, MiddleIndex: Integer;
begin
  Result := False;
  for PreviousIndex := 1 to GetArrayLength(PreviousFiles) - 1 do
  begin
    LowIndex := 1;
    HighIndex := GetArrayLength(IncomingFiles);
    while LowIndex < HighIndex do
    begin
      MiddleIndex := LowIndex + (HighIndex - LowIndex) div 2;
      if CompareManagedPathsOrdinal(
        IncomingFiles[MiddleIndex], PreviousFiles[PreviousIndex]
      ) < 0 then
        LowIndex := MiddleIndex + 1
      else
        HighIndex := MiddleIndex;
    end;
    if (LowIndex < GetArrayLength(IncomingFiles)) and
       IsPathShapeConflict(
         PreviousFiles[PreviousIndex], IncomingFiles[LowIndex]
       ) then
    begin
      Description :=
        PreviousFiles[PreviousIndex] + ' conflicts with ' +
        IncomingFiles[LowIndex];
      Result := True;
      Exit;
    end;
    if (LowIndex > 1) and
       IsPathShapeConflict(
         PreviousFiles[PreviousIndex], IncomingFiles[LowIndex - 1]
       ) then
    begin
      Description :=
        PreviousFiles[PreviousIndex] + ' conflicts with ' +
        IncomingFiles[LowIndex - 1];
      Result := True;
      Exit;
    end;
  end;
end;

procedure RemoveEmptyManagedParents(const FilePath: String);
var
  AppRoot, Parent: String;
begin
  AppRoot := RemoveBackslashUnlessRoot(ExpandConstant('{app}'));
  Parent := ExtractFileDir(FilePath);
  while Length(Parent) > Length(AppRoot) do
  begin
    if not RemoveDir(Parent) then
      Exit;
    Parent := ExtractFileDir(Parent);
  end;
end;

procedure RemoveObsoleteManagedFiles(
  var PreviousFiles, IncomingFiles: TArrayOfString
);
var
  PreviousIndex, IncomingIndex, Comparison: Integer;
  RelativePath, TargetRelativePath, Target: String;
begin
  PreviousIndex := 1;
  IncomingIndex := 1;
  while PreviousIndex < GetArrayLength(PreviousFiles) do
  begin
    RelativePath := PreviousFiles[PreviousIndex];
    while IncomingIndex < GetArrayLength(IncomingFiles) do
    begin
      Comparison := CompareManagedPathsOrdinal(
        IncomingFiles[IncomingIndex], RelativePath
      );
      if Comparison < 0 then
        IncomingIndex := IncomingIndex + 1
      else
        Break;
    end;
    if (IncomingIndex >= GetArrayLength(IncomingFiles)) or
      (CompareManagedPathsOrdinal(
        IncomingFiles[IncomingIndex], RelativePath
      ) <> 0) then
    begin
      NormalizeManagedPath(RelativePath, TargetRelativePath);
      Target := AddBackslash(ExpandConstant('{app}')) + TargetRelativePath;
      if PathContainsReparsePoint(TargetRelativePath) then
      begin
        Log('Preserved obsolete installer-managed file because its path is not safely contained: ' + RelativePath);
      end
      else if (not FileExists(Target)) or DeleteFile(Target) then
      begin
        Log('Removed obsolete installer-managed file: ' + RelativePath);
        RemoveEmptyManagedParents(Target);
      end
      else
      begin
        Log('Could not remove obsolete installer-managed file; it is preserved without current-version ownership: ' + RelativePath);
      end;
    end;
    PreviousIndex := PreviousIndex + 1;
  end;
end;

procedure FinalizeInstallManifest;
var
  ManifestPath, InstalledHash: String;
begin
  ManifestPath := AddBackslash(ExpandConstant('{app}')) + ManagedManifestName;
  InstalledHash := Lowercase(GetSHA256OfFile(ManifestPath));
  if InstalledHash <> TrustedIncomingManifestSHA256 then
    RaiseException('The installed managed-file manifest does not match the compiled release.');

  if CachedPreviousIsValid then
    RemoveObsoleteManagedFiles(CachedPreviousFiles, CachedIncomingFiles)
  else
    Log('No integrity-verified previous managed-file manifest; existing files are preserved.');
end;

procedure CachePreviousInstallManifest;
var
  ManifestPath, ExpectedHash, ActualHash: String;
begin
  CachedPreviousIsValid := False;
  ManifestPath := AddBackslash(ExpandConstant('{app}')) + ManagedManifestName;
  ExpectedHash := GetPreviousData('InstalledManifestSHA256', '');
  if not FileExists(ManifestPath) then
    Exit;
  if not IsCanonicalSHA256(ExpectedHash) then
  begin
    Log('Previous managed-file manifest has no trusted hash; cleanup is disabled.');
    Exit;
  end;
  try
    ActualHash := Lowercase(GetSHA256OfFile(ManifestPath));
  except
    Log(
      'Previous managed-file manifest could not be hashed; cleanup is disabled: ' +
      GetExceptionMessage
    );
    Exit;
  end;
  if ActualHash <> ExpectedHash then
  begin
    Log('Previous managed-file manifest hash mismatch; cleanup is disabled.');
    Exit;
  end;
  if LoadStringsFromFile(ManifestPath, CachedPreviousFiles) then
    CachedPreviousIsValid := IsValidManagedManifest(
      CachedPreviousFiles, 'Previous managed-file manifest'
    );
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  IncomingManifestPath, IncomingHash, Conflict: String;
begin
  if InstallerPrepared then
  begin
    Result := InstallerPreparationResult;
    Exit;
  end;
  InstallerPreparationResult := '';

  ExtractTemporaryFile(ManagedManifestName);
  IncomingManifestPath :=
    AddBackslash(ExpandConstant('{tmp}')) + ManagedManifestName;
  try
    IncomingHash := Lowercase(GetSHA256OfFile(IncomingManifestPath));
  except
    InstallerPreparationResult :=
      'The installer managed-file manifest could not be verified.';
    InstallerPrepared := True;
    Result := InstallerPreparationResult;
    Exit;
  end;
  if IncomingHash <> TrustedIncomingManifestSHA256 then
  begin
    InstallerPreparationResult :=
      'The installer managed-file manifest does not match the compiled release.';
    InstallerPrepared := True;
    Result := InstallerPreparationResult;
    Exit;
  end;
  if not LoadStringsFromFile(IncomingManifestPath, CachedIncomingFiles) or
     not IsValidManagedManifest(
       CachedIncomingFiles, 'Incoming managed-file manifest'
     ) then
  begin
    InstallerPreparationResult :=
      'The installer managed-file manifest is invalid.';
    InstallerPrepared := True;
    Result := InstallerPreparationResult;
    Exit;
  end;
  CachePreviousInstallManifest;
  if CachedPreviousIsValid and
     FindPathShapeConflict(CachedPreviousFiles, CachedIncomingFiles, Conflict) then
  begin
    InstallerPreparationResult :=
      'This release changes a managed file into a directory or a directory into a file. ' +
      'Uninstall the existing version before installing this release. Conflict: ' +
      Conflict;
  end;
  InstallerPrepared := True;
  Result := InstallerPreparationResult;
end;

function NormalizeVersionForComparison(
  const Value: String;
  var Normalized: String
): Boolean;
var
  I, DotCount: Integer;
begin
  Result := False;
  Normalized := Trim(Value);
  DotCount := 0;
  for I := 1 to Length(Normalized) do
  begin
    if Normalized[I] = '.' then
      DotCount := DotCount + 1
    else if (Normalized[I] < '0') or (Normalized[I] > '9') then
      Exit;
  end;
  if (Normalized = '') or (DotCount > 3) then
    Exit;
  while DotCount < 3 do
  begin
    Normalized := Normalized + '.0';
    DotCount := DotCount + 1;
  end;
  Result := True;
end;

function InitializeSetup: Boolean;
var
  PreviousText, CurrentText: String;
  PreviousVersion, CurrentVersion: Int64;
begin
  Result := True;
  PreviousText := GetPreviousData('InstalledAppVersion', '');
  if PreviousText = '' then
    Exit;
  if not NormalizeVersionForComparison(PreviousText, PreviousText) or
     not NormalizeVersionForComparison('{#MyAppVersion}', CurrentText) or
     not StrToVersion(PreviousText, PreviousVersion) or
     not StrToVersion(CurrentText, CurrentVersion) then
  begin
    Log('Could not compare installed and incoming Automadin versions; downgrade guard is conservative.');
    Result := False;
    Exit;
  end;
  if ComparePackedVersion(PreviousVersion, CurrentVersion) > 0 then
  begin
    SuppressibleMsgBox(
      'A newer Automadin version is already installed. Uninstall it before installing this older version.',
      mbCriticalError, MB_OK, IDOK
    );
    Result := False;
  end;
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(
    PreviousDataKey, 'InstalledAppVersion', '{#MyAppVersion}'
  );
  SetPreviousData(
    PreviousDataKey, 'InstalledManifestSHA256',
    TrustedIncomingManifestSHA256
  );
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    FinalizeInstallManifest;
end;
