param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000
)

$Python = 'D:\tools\anaconda\python.exe'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "找不到 Python：$Python。也可以直接使用项目约定的 Python 路径运行 scripts\show_lan_url.py。"
}

& $Python -B (Join-Path $PSScriptRoot 'show_lan_url.py') --port $Port
exit $LASTEXITCODE
