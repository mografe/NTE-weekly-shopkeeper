# Windows 内置 OCR (WinRT Windows.Media.Ocr) 调用封装。
#
# 关键点：沙箱进程处于低完整性级别时，StorageFile.GetFileFromPathAsync 会报
# UnauthorizedAccessException，因此这里不走文件系统，而是用普通 .NET IO 读字节，
# 再灌进 InMemoryRandomAccessStream 交给 BitmapDecoder。
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File ocr.ps1 -Path <图片> [-Scale 2] [-Lang zh-Hans-CN]
# 输出 (stdout, 单行 JSON):
#   {"text":"...","lines":[{"text":"...","words":[{"text":"..","x":..,"y":..,"w":..,"h":..}]}]}
#   坐标已按 -Scale 还原到原图尺度
param(
    [Parameter(Mandatory = $true)][string]$Path,
    [double]$Scale = 1,
    [string]$Lang = 'zh-Hans-CN'
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
Add-Type -AssemblyName System.Drawing

$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.DataWriter, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]

function Await($op, $type) {
    $m = $asTaskGeneric.MakeGenericMethod($type)
    $t = $m.Invoke($null, @($op))
    [void]$t.Wait(-1)
    return $t.Result
}

# 读字节（可选放大），得到 PNG 字节流
$full = (Resolve-Path -LiteralPath $Path).Path
if ($Scale -ne 1) {
    $src = [System.Drawing.Image]::FromFile($full)
    $w = [int]($src.Width * $Scale); $h = [int]($src.Height * $Scale)
    $bmp = New-Object System.Drawing.Bitmap $w, $h
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $g.DrawImage($src, 0, 0, $w, $h)
    $g.Dispose(); $src.Dispose()
    $msImg = New-Object System.IO.MemoryStream
    $bmp.Save($msImg, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    $bytes = $msImg.ToArray()
    $msImg.Dispose()
} else {
    $bytes = [System.IO.File]::ReadAllBytes($full)
}

$ms = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
$dw = New-Object Windows.Storage.Streams.DataWriter($ms)
$dw.WriteBytes($bytes)
[void](Await ($dw.StoreAsync()) ([uint32]))
[void]$dw.DetachStream()
[void]$ms.Seek(0)

$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($ms)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new($Lang))
if (-not $engine) { throw "无法创建 OCR 引擎: $Lang (该语言包可能未安装)" }

$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

$lines = @()
foreach ($ln in $result.Lines) {
    $words = @()
    foreach ($wd in $ln.Words) {
        $words += [pscustomobject]@{
            text = $wd.Text
            x    = [math]::Round($wd.BoundingRect.X / $Scale, 1)
            y    = [math]::Round($wd.BoundingRect.Y / $Scale, 1)
            w    = [math]::Round($wd.BoundingRect.Width / $Scale, 1)
            h    = [math]::Round($wd.BoundingRect.Height / $Scale, 1)
        }
    }
    $lines += [pscustomobject]@{ text = $ln.Text; words = $words }
}

[pscustomobject]@{ text = $result.Text; lines = $lines } | ConvertTo-Json -Depth 8 -Compress

$ms.Dispose()
