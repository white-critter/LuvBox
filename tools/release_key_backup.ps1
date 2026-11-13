# Back up or restore the key that signs LuvBox releases (see tools\release_key.ps1).
#
# The backup is the key's private part as an encrypted PKCS#8 file. It is
# protected by a passphrase that only the person at the keyboard types: for the
# release key this script reads the passphrase from the console and nowhere
# else, so it cannot be run unattended. Whoever has the file AND the passphrase
# can sign releases that installed versions accept. Keep the file away from the
# repository, release files and logs; this script refuses to write it there.
#
#   -Export <file>   write a backup of the key (asks for a new passphrase twice),
#                    then read it back to prove the file and passphrase work
#   -Verify <file>   check that a backup opens with its passphrase and which key it holds
#   -Import <file>   put a backup into this Windows account's key store (a new PC,
#                    or after Windows was reinstalled); refused while a key is there
param(
    [string]$Export = '',
    [string]$Verify = '',
    [string]$Import = '',
    [string]$Name = 'LuvBoxReleaseSigning'
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Core
Add-Type -AssemblyName System.Security

$ReleaseName = 'LuvBoxReleaseSigning'
$TestPrefix = 'LuvBoxReleaseSigningTest-'
if ($Name -ne $ReleaseName -and -not $Name.StartsWith($TestPrefix)) {
    throw "Unexpected key name: $Name"
}
$MinimumLength = 12
# Work done per passphrase guess by someone who has the file.
$Iterations = 200000

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Security;
using System.Security.Cryptography;
using System.Text;

public static class LuvBoxKeyBackup
{
    [StructLayout(LayoutKind.Sequential)]
    struct NCryptBuffer { public int cbBuffer; public int BufferType; public IntPtr pvBuffer; }

    [StructLayout(LayoutKind.Sequential)]
    struct NCryptBufferDesc { public int ulVersion; public int cBuffers; public IntPtr pBuffers; }

    const string Provider = "Microsoft Software Key Storage Provider";
    const string Pkcs8 = "PKCS8_PRIVATEKEY";
    // PKCS#12 password-based encryption with three-key triple DES: the one
    // encrypted export every supported Windows offers for a stored key.
    const string Encryption = "1.2.840.113549.1.12.1.3\0";
    const int PkcsAlgOid = 41, PkcsAlgParam = 42, PkcsKeyName = 45, PkcsSecret = 46;
    const int DoNotFinalize = 0x400;
    const int Persist = unchecked((int)0x80000000);
    const int AllowExport = 1, AllowSigning = 2;

    [DllImport("ncrypt.dll", CharSet = CharSet.Unicode)]
    static extern int NCryptOpenStorageProvider(out IntPtr provider, string name, int flags);

    [DllImport("ncrypt.dll", CharSet = CharSet.Unicode)]
    static extern int NCryptOpenKey(IntPtr provider, out IntPtr key, string name, int legacyKeySpec, int flags);

    [DllImport("ncrypt.dll", CharSet = CharSet.Unicode)]
    static extern int NCryptExportKey(IntPtr key, IntPtr exportKey, string blobType, ref NCryptBufferDesc parameters,
                                      byte[] output, int outputSize, out int resultSize, int flags);

    [DllImport("ncrypt.dll", CharSet = CharSet.Unicode)]
    static extern int NCryptImportKey(IntPtr provider, IntPtr importKey, string blobType, ref NCryptBufferDesc parameters,
                                      out IntPtr key, byte[] data, int dataSize, int flags);

    [DllImport("ncrypt.dll", CharSet = CharSet.Unicode)]
    static extern int NCryptSetProperty(IntPtr handle, string property, byte[] input, int inputSize, int flags);

    [DllImport("ncrypt.dll")]
    static extern int NCryptFinalizeKey(IntPtr key, int flags);

    [DllImport("ncrypt.dll")]
    static extern int NCryptDeleteKey(IntPtr key, int flags);

    [DllImport("ncrypt.dll")]
    static extern int NCryptFreeObject(IntPtr handle);

    static void Check(int status, string what)
    {
        if (status != 0)
            throw new InvalidOperationException(what + " (0x" + status.ToString("x8") + ")");
    }

    static void Put(IntPtr buffers, int index, int size, int kind, IntPtr data)
    {
        NCryptBuffer buffer = new NCryptBuffer();
        buffer.cbBuffer = size;
        buffer.BufferType = kind;
        buffer.pvBuffer = data;
        Marshal.StructureToPtr(buffer, IntPtr.Add(buffers, index * Marshal.SizeOf(typeof(NCryptBuffer))), false);
    }

    /// <summary>Whether two passphrases are the same, without making either a managed string.</summary>
    public static bool Same(SecureString first, SecureString second)
    {
        IntPtr one = IntPtr.Zero, other = IntPtr.Zero;
        try
        {
            if (first.Length != second.Length)
                return false;
            one = Marshal.SecureStringToGlobalAllocUnicode(first);
            other = Marshal.SecureStringToGlobalAllocUnicode(second);
            int difference = 0;
            for (int index = 0; index < first.Length * 2; index++)
                difference |= Marshal.ReadByte(one, index) ^ Marshal.ReadByte(other, index);
            return difference == 0;
        }
        finally
        {
            if (one != IntPtr.Zero) Marshal.ZeroFreeGlobalAllocUnicode(one);
            if (other != IntPtr.Zero) Marshal.ZeroFreeGlobalAllocUnicode(other);
        }
    }

    /// <summary>The key's private part, encrypted under the passphrase (PKCS#8 EncryptedPrivateKeyInfo, DER).</summary>
    public static byte[] Export(string keyName, SecureString passphrase, int iterations)
    {
        IntPtr provider = IntPtr.Zero, key = IntPtr.Zero, secret = IntPtr.Zero;
        IntPtr algorithm = IntPtr.Zero, settings = IntPtr.Zero, buffers = IntPtr.Zero;
        try
        {
            Check(NCryptOpenStorageProvider(out provider, Provider, 0), "The key store could not be opened");
            Check(NCryptOpenKey(provider, out key, keyName, 0, 0), "The key could not be opened");
            secret = Marshal.SecureStringToGlobalAllocUnicode(passphrase);
            byte[] name = Encoding.ASCII.GetBytes(Encryption);
            algorithm = Marshal.AllocHGlobal(name.Length);
            Marshal.Copy(name, 0, algorithm, name.Length);
            byte[] salt = new byte[8];
            using (RNGCryptoServiceProvider random = new RNGCryptoServiceProvider())
                random.GetBytes(salt);
            // CRYPT_PKCS12_PBE_PARAMS: the iteration count, the salt's length, then the salt.
            settings = Marshal.AllocHGlobal(8 + salt.Length);
            Marshal.WriteInt32(settings, 0, iterations);
            Marshal.WriteInt32(settings, 4, salt.Length);
            Marshal.Copy(salt, 0, IntPtr.Add(settings, 8), salt.Length);
            buffers = Marshal.AllocHGlobal(3 * Marshal.SizeOf(typeof(NCryptBuffer)));
            Put(buffers, 0, (passphrase.Length + 1) * 2, PkcsSecret, secret);
            Put(buffers, 1, name.Length, PkcsAlgOid, algorithm);
            Put(buffers, 2, 8 + salt.Length, PkcsAlgParam, settings);
            NCryptBufferDesc parameters = new NCryptBufferDesc();
            parameters.ulVersion = 0;
            parameters.cBuffers = 3;
            parameters.pBuffers = buffers;
            int size;
            Check(NCryptExportKey(key, IntPtr.Zero, Pkcs8, ref parameters, null, 0, out size, 0),
                  "The key could not be exported");
            byte[] output = new byte[size];
            Check(NCryptExportKey(key, IntPtr.Zero, Pkcs8, ref parameters, output, output.Length, out size, 0),
                  "The key could not be exported");
            if (size != output.Length)
                Array.Resize(ref output, size);
            return output;
        }
        finally
        {
            if (secret != IntPtr.Zero) Marshal.ZeroFreeGlobalAllocUnicode(secret);
            if (algorithm != IntPtr.Zero) Marshal.FreeHGlobal(algorithm);
            if (settings != IntPtr.Zero) Marshal.FreeHGlobal(settings);
            if (buffers != IntPtr.Zero) Marshal.FreeHGlobal(buffers);
            if (key != IntPtr.Zero) NCryptFreeObject(key);
            if (provider != IntPtr.Zero) NCryptFreeObject(provider);
        }
    }

    /// <summary>Put a backup into the key store under a name that is not in use.</summary>
    public static void Import(string keyName, byte[] backup, SecureString passphrase)
    {
        IntPtr provider = IntPtr.Zero, key = IntPtr.Zero, secret = IntPtr.Zero, name = IntPtr.Zero, buffers = IntPtr.Zero;
        bool finished = false;
        try
        {
            Check(NCryptOpenStorageProvider(out provider, Provider, 0), "The key store could not be opened");
            secret = Marshal.SecureStringToGlobalAllocUnicode(passphrase);
            name = Marshal.StringToHGlobalUni(keyName);
            buffers = Marshal.AllocHGlobal(2 * Marshal.SizeOf(typeof(NCryptBuffer)));
            Put(buffers, 0, (passphrase.Length + 1) * 2, PkcsSecret, secret);
            Put(buffers, 1, (keyName.Length + 1) * 2, PkcsKeyName, name);
            NCryptBufferDesc parameters = new NCryptBufferDesc();
            parameters.ulVersion = 0;
            parameters.cBuffers = 2;
            parameters.pBuffers = buffers;
            Check(NCryptImportKey(provider, IntPtr.Zero, Pkcs8, ref parameters, out key, backup, backup.Length, DoNotFinalize),
                  "The backup did not open: wrong passphrase, a damaged file, or a key of that name already exists");
            // The same rules the key was made with: it signs, and it can be backed up again (encrypted only).
            Check(NCryptSetProperty(key, "Export Policy", BitConverter.GetBytes(AllowExport), 4, Persist),
                  "The restored key's export rule could not be set");
            Check(NCryptSetProperty(key, "Key Usage", BitConverter.GetBytes(AllowSigning), 4, Persist),
                  "The restored key's use could not be set");
            Check(NCryptFinalizeKey(key, 0), "The restored key could not be stored");
            finished = true;
        }
        finally
        {
            if (secret != IntPtr.Zero) Marshal.ZeroFreeGlobalAllocUnicode(secret);
            if (name != IntPtr.Zero) Marshal.FreeHGlobal(name);
            if (buffers != IntPtr.Zero) Marshal.FreeHGlobal(buffers);
            if (key != IntPtr.Zero)
            {
                // A key that was not stored completely is removed again; deleting also releases the handle.
                if (finished) NCryptFreeObject(key); else NCryptDeleteKey(key, 0);
            }
            if (provider != IntPtr.Zero) NCryptFreeObject(provider);
        }
    }
}
'@

function Get-PublicPart([string]$keyName) {
    $key = [System.Security.Cryptography.CngKey]::Open($keyName)
    try {
        $rsa = New-Object System.Security.Cryptography.RSACng($key)
        try { return $rsa.ExportParameters($false) } finally { $rsa.Dispose() }
    } finally {
        $key.Dispose()
    }
}

function Get-KeyId([byte[]]$modulus) {
    # The same 16 characters luvbox/release_signature.py calls a key's identifier.
    $hash = [System.Security.Cryptography.SHA256]::Create()
    try {
        return (($hash.ComputeHash($modulus) | ForEach-Object { $_.ToString('x2') }) -join '').Substring(0, 16)
    } finally {
        $hash.Dispose()
    }
}

function Same-Bytes([byte[]]$first, [byte[]]$second) {
    if ($first.Length -ne $second.Length) { return $false }
    for ($index = 0; $index -lt $first.Length; $index++) {
        if ($first[$index] -ne $second[$index]) { return $false }
    }
    return $true
}

function Read-Passphrase([bool]$twice) {
    if ($Name.StartsWith($TestPrefix) -and $env:LUVBOX_TEST_KEY_PASSPHRASE) {
        # Keys made for a test only. The release key's passphrase comes from the keyboard below and nowhere else.
        return (ConvertTo-SecureString $env:LUVBOX_TEST_KEY_PASSPHRASE -AsPlainText -Force)
    }
    $first = Read-Host -AsSecureString "Passphrase for the backup (at least $MinimumLength characters)"
    if ($twice) {
        $again = Read-Host -AsSecureString 'The same passphrase once more'
        if (-not [LuvBoxKeyBackup]::Same($first, $again)) { throw 'The two passphrases differ. Nothing was written.' }
    }
    return $first
}

function Remove-TrialKey([string]$trial) {
    if ($trial.StartsWith($TestPrefix) -and [System.Security.Cryptography.CngKey]::Exists($trial)) {
        $key = [System.Security.Cryptography.CngKey]::Open($trial)
        $key.Delete()
    }
}

function Read-Backup([string]$file, [System.Security.SecureString]$passphrase) {
    # Open the backup as a short-lived key of its own name and return its public part.
    $trial = $TestPrefix + 'read-' + [Guid]::NewGuid().ToString('N').Substring(0, 12)
    try {
        [LuvBoxKeyBackup]::Import($trial, [System.IO.File]::ReadAllBytes($file), $passphrase)
        return (Get-PublicPart $trial)
    } finally {
        Remove-TrialKey $trial
    }
}

function Resolve-Outside([string]$path) {
    $full = [System.IO.Path]::GetFullPath($path)
    $project = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\') + '\'
    if (($full + '\').StartsWith($project, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'The backup must not be inside the project folder (it could end up in Git or a release). Choose another place.'
    }
    return $full
}

if ($Export) {
    $target = Resolve-Outside $Export
    if (Test-Path -LiteralPath $target) { throw "There is already a file at $target. Nothing was written." }
    if (-not (Test-Path -LiteralPath (Split-Path -Parent $target) -PathType Container)) {
        throw 'The folder for the backup does not exist.'
    }
    if (-not [System.Security.Cryptography.CngKey]::Exists($Name)) { throw "This Windows account has no key named $Name." }
    $passphrase = Read-Passphrase $true
    if ($passphrase.Length -lt $MinimumLength) { throw "The passphrase needs at least $MinimumLength characters. Nothing was written." }
    $bytes = [LuvBoxKeyBackup]::Export($Name, $passphrase, $Iterations)
    $stream = New-Object System.IO.FileStream($target, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write)
    try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
    try {
        $read = Read-Backup $target $passphrase
        if (-not (Same-Bytes $read.Modulus (Get-PublicPart $Name).Modulus)) { throw 'The file read back as another key.' }
    } catch {
        Remove-Item -LiteralPath $target -Force
        throw ('The backup did not read back and was removed: ' + $_.Exception.Message)
    }
    Write-Output ("Backup written and read back: $target (" + $bytes.Length + ' bytes), key id ' + (Get-KeyId $read.Modulus) + '.')
    Write-Output 'Keep the file and the passphrase in separate places. Without the passphrase the file cannot be used.'
    exit 0
}

if ($Verify) {
    $source = [System.IO.Path]::GetFullPath($Verify)
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "No backup file at $source." }
    $read = Read-Backup $source (Read-Passphrase $false)
    $line = 'The backup opens with this passphrase: RSA ' + ($read.Modulus.Length * 8) + ', key id ' + (Get-KeyId $read.Modulus) + '.'
    if ([System.Security.Cryptography.CngKey]::Exists($Name)) {
        if (Same-Bytes $read.Modulus (Get-PublicPart $Name).Modulus) { $line += " It is the same key as $Name in this account." }
        else { $line += " It is NOT the key named $Name in this account." }
    }
    Write-Output $line
    exit 0
}

if ($Import) {
    $source = [System.IO.Path]::GetFullPath($Import)
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "No backup file at $source." }
    if ([System.Security.Cryptography.CngKey]::Exists($Name)) {
        throw "This Windows account already has a key named $Name. It is left as it is."
    }
    [LuvBoxKeyBackup]::Import($Name, [System.IO.File]::ReadAllBytes($source), (Read-Passphrase $false))
    $restored = Get-PublicPart $Name
    Write-Output ('Restored ' + $Name + ': RSA ' + ($restored.Modulus.Length * 8) + ', key id ' + (Get-KeyId $restored.Modulus) + '.')
    Write-Output 'Check that the application trusts it: python tools\release_key.py --check'
    exit 0
}

throw 'Use -Export <file>, -Verify <file> or -Import <file>.'
