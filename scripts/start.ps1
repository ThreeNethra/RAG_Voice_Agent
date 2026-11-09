# Start the voice agent from the project folder.
#   .\scripts\start.ps1                    plain mode (no model: reads back the best passages)
#   .\scripts\start.ps1 -Backend api       Gemini, key read from the .env file (see README)
#   .\scripts\start.ps1 -Backend ollama    a local model through Ollama
# Optional: -Model medium.en   a bigger, more accurate speech recogniser
param([string]$Backend = "extractive", [string]$Model = "small.en")
Set-Location (Split-Path $PSScriptRoot -Parent)
& .\.venv\Scripts\python.exe -m voicecall --backend $Backend --stt-model $Model
