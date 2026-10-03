# 用 Windows SAPI 合成一段测试语音，存成 WAV。
# 用途：不依赖麦克风和热键，单独验证 ASR 引擎通路。
#
#   powershell -ExecutionPolicy Bypass -File tools\make-test-audio.ps1
#
# 输出：%TEMP%\voicepill-test.wav

Add-Type -AssemblyName System.Speech

$text = "这是一段测试语音，用来验证语音转写引擎是否工作正常。今天天气不错。"
$out  = Join-Path $env:TEMP "voicepill-test.wav"

$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer

# 优先挑中文发音人；没有就退回默认
$zh = $synth.GetInstalledVoices() |
      Where-Object { $_.VoiceInfo.Culture.Name -like "zh-*" } |
      Select-Object -First 1
if ($zh) {
    $synth.SelectVoice($zh.VoiceInfo.Name)
    Write-Output ("发音人: " + $zh.VoiceInfo.Name + " (" + $zh.VoiceInfo.Culture.Name + ")")
} else {
    Write-Output ("发音人: 默认（未找到中文发音人）")
    Write-Output "已安装的发音人："
    $synth.GetInstalledVoices() | ForEach-Object {
        Write-Output ("  - " + $_.VoiceInfo.Name + " / " + $_.VoiceInfo.Culture.Name)
    }
}

$synth.Rate = 0
$synth.SetOutputToWaveFile($out)
$synth.Speak($text)
$synth.SetOutputToNull()
$synth.Dispose()

$fi = Get-Item $out
Write-Output ("已生成: " + $fi.FullName)
Write-Output ("大小: " + $fi.Length + " 字节")
