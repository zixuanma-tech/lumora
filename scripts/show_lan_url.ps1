param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000
)

$Python = 'D:\tools\anaconda\python.exe'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "找不到 Python：$Python。请先在脚本中改成自己的 Python 路径。"
}

& $Python -B (Join-Path $PSScriptRoot 'show_lan_url.py') --port $Port
exit $LASTEXITCODE
