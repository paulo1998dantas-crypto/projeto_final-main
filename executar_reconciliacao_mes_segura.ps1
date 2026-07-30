<#
Executa a conciliação MES legado -> ERP pago sem salvar URLs ou senhas em
arquivo, histórico do PowerShell ou .env.local. O programa Python continua
somente leitura; este script mantém as duas URLs apenas na memória do processo.
#>

$ErrorActionPreference = 'Stop'
$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = 'C:\Users\PRODUCAO-2.0\AppData\Local\Programs\Python\Python314\python.exe'

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Python não encontrado em: $pythonPath"
}

function ConvertFrom-SecureText([Security.SecureString]$Value) {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

Write-Host ''
Write-Host 'Conciliação MES legado -> ERP compartilhado (somente leitura)' -ForegroundColor Cyan
Write-Host 'As URLs não serão salvas. Não cole nenhuma credencial no chat.' -ForegroundColor Yellow
Write-Host ''

$legacySecure = Read-Host 'Cole a DATABASE_URL completa do MES legado' -AsSecureString
$targetSecure = Read-Host 'Cole a DATABASE_URL completa do ERP pago' -AsSecureString
$legacyUrl = $null
$targetUrl = $null

try {
    $legacyUrl = ConvertFrom-SecureText $legacySecure
    $targetUrl = ConvertFrom-SecureText $targetSecure
    if ($legacyUrl -notmatch '^postgres(ql)?(\+[a-z0-9_]+)?://') {
        throw 'A URL do MES legado não parece uma connection string PostgreSQL.'
    }
    if ($targetUrl -notmatch '^postgres(ql)?(\+[a-z0-9_]+)?://') {
        throw 'A URL do ERP pago não parece uma connection string PostgreSQL.'
    }

    $env:MES_LEGACY_DATABASE_URL = $legacyUrl
    $env:ERP_TARGET_DATABASE_URL = $targetUrl

    $artifactDirectory = Join-Path $scriptDirectory 'artifacts'
    $reportPath = Join-Path $artifactDirectory 'mes_reconciliation.json'
    & $pythonPath (Join-Path $scriptDirectory 'mes_legacy_reconciliation.py') --report $reportPath
    if ($LASTEXITCODE -ne 0) {
        throw "A conciliação terminou com código $LASTEXITCODE. Nenhum dado foi alterado."
    }

    Write-Host ''
    Write-Host "Relatório gerado: $reportPath" -ForegroundColor Green
    Write-Host 'Envie somente esse JSON para análise; não envie as URLs ou senhas.' -ForegroundColor Green
}
finally {
    Remove-Item Env:MES_LEGACY_DATABASE_URL -ErrorAction SilentlyContinue
    Remove-Item Env:ERP_TARGET_DATABASE_URL -ErrorAction SilentlyContinue
    Remove-Variable legacyUrl, targetUrl -ErrorAction SilentlyContinue
}
