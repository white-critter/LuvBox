# The key that signs LuvBox releases.
#
# It lives in this Windows account's key store (CNG, the Microsoft Software Key
# Storage Provider). The private part never leaves that store through this
# script: nothing here writes it to a file, the console or the repository.
# Installed versions carry only the public part (luvbox/release_signature.py).
#
#   -Create        make the key if this account has none, then print the public part
#   -Public        print the public part as JSON: {"bits", "modulus", "exponent"} (hex)
#   -Sign <file>   sign that file's bytes (RSA, PKCS#1 v1.5, SHA-256), print base64
#   -Delete        remove a key made for a test; refused for the release key itself
param(
    [switch]$Create,
    [switch]$Public,
    [string]$Sign = '',
    [switch]$Delete,
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

function Hex([byte[]]$bytes) {
    return (($bytes | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Open-Key {
    if (-not [System.Security.Cryptography.CngKey]::Exists($Name)) {
        throw "This Windows account has no key named $Name. Create it once with: tools\release_key.ps1 -Create"
    }
    return [System.Security.Cryptography.CngKey]::Open($Name)
}

function Show-Public($key) {
    $rsa = New-Object System.Security.Cryptography.RSACng($key)
    try {
        $parts = $rsa.ExportParameters($false)
        $json = '{"bits": ' + $rsa.KeySize + ', "modulus": "' + (Hex $parts.Modulus) + '", "exponent": "' + (Hex $parts.Exponent) + '"}'
        Write-Output $json
    } finally {
        $rsa.Dispose()
    }
}

if ($Create) {
    if (-not [System.Security.Cryptography.CngKey]::Exists($Name)) {
        $options = New-Object System.Security.Cryptography.CngKeyCreationParameters
        # The owner may back the key up later (an encrypted export); it is never exported in the clear.
        $options.ExportPolicy = [System.Security.Cryptography.CngExportPolicies]::AllowExport
        $options.KeyUsage = [System.Security.Cryptography.CngKeyUsages]::Signing
        $length = New-Object System.Security.Cryptography.CngProperty(
            'Length', [BitConverter]::GetBytes(3072), [System.Security.Cryptography.CngPropertyOptions]::None)
        $options.Parameters.Add($length)
        $made = [System.Security.Cryptography.CngKey]::Create(
            [System.Security.Cryptography.CngAlgorithm]::Rsa, $Name, $options)
        $made.Dispose()
    }
    $key = Open-Key
    try { Show-Public $key } finally { $key.Dispose() }
    exit 0
}

if ($Public) {
    $key = Open-Key
    try { Show-Public $key } finally { $key.Dispose() }
    exit 0
}

if ($Sign) {
    $bytes = [System.IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $Sign).Path)
    $key = Open-Key
    try {
        $rsa = New-Object System.Security.Cryptography.RSACng($key)
        try {
            $signature = $rsa.SignData($bytes, [System.Security.Cryptography.HashAlgorithmName]::SHA256,
                [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)
            Write-Output ([Convert]::ToBase64String($signature))
        } finally {
            $rsa.Dispose()
        }
    } finally {
        $key.Dispose()
    }
    exit 0
}

if ($Delete) {
    if (-not $Name.StartsWith($TestPrefix)) {
        throw 'Only a key made for a test can be removed here.'
    }
    if ([System.Security.Cryptography.CngKey]::Exists($Name)) {
        $key = [System.Security.Cryptography.CngKey]::Open($Name)
        $key.Delete()
    }
    exit 0
}

throw 'Use -Create, -Public, -Sign <file> or -Delete.'
