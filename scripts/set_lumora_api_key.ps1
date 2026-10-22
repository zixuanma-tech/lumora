param(
    [switch]$PersistUser,
    [switch]$Clear
)

$ErrorActionPreference = "Stop"
$variableName = "LUMORA_LLM_API_KEY"

if ($Clear) {
    Remove-Item Env:$variableName -ErrorAction SilentlyContinue
    if ($PersistUser) {
        [Environment]::SetEnvironmentVariable($variableName, $null, "User")
    }
    Write-Host "已清除当前 PowerShell 会话中的 $variableName。"
    if ($PersistUser) {
        Write-Host "已清除当前用户环境变量中的 $variableName。"
    }
    exit 0
}

$secureKey = Read-Host -Prompt "请输入 Paratera API Key（输入不会回显）" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
try {
    $apiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

if ([string]::IsNullOrWhiteSpace($apiKey)) {
    throw "API Key 不能为空。"
}

$env:LUMORA_LLM_API_KEY = $apiKey
if ($PersistUser) {
    [Environment]::SetEnvironmentVariable($variableName, $apiKey, "User")
}

Write-Host "当前 PowerShell 会话已设置 $variableName。"
if ($PersistUser) {
    Write-Host "已保存到当前 Windows 用户环境变量。"
} else {
    Write-Host "仅当前 PowerShell 会话生效；如需持久保存，请加 -PersistUser。"
}
Write-Host "密钥未写入 llm_config.json。"
