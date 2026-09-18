# Installation

## Requirements

- Python 3.10 or newer
- Git
- Ollama reachable locally or over your own network
- an Ollama model

Wynxo has no compiled runtime dependency.

## Linux, macOS and Termux

~~~bash
curl -fsSL https://raw.githubusercontent.com/wynxo/wynxo-cli/main/get.sh | sh
~~~

The installer keeps the source under ~/.wynxo-src, creates its virtual
environment and exposes the wynxo command on PATH.

## Windows

~~~powershell
irm https://raw.githubusercontent.com/wynxo/wynxo-cli/main/get.ps1 | iex
~~~

The Windows launcher invokes the virtual-environment Python directly. This
avoids depending on shell activation for normal use.

## Ollama

Start Ollama and install whichever model you want:

~~~bash
ollama serve
ollama pull qwen3-coder:30b
~~~

Then run wynxo. The first-run flow asks for the endpoint and model.

A large model does not need to run entirely on the same machine as the terminal
client. A common setup is a laptop or Termux client talking to an Ollama server
on a desktop on the same trusted network.

## Updating

Run the same installer command again. It updates the source checkout and
reuses the managed installation.

## Removing Wynxo

Linux, macOS and Termux:

~~~bash
curl -fsSL https://raw.githubusercontent.com/wynxo/wynxo-cli/main/rm.sh | sh
~~~

Windows PowerShell:

~~~powershell
irm https://raw.githubusercontent.com/wynxo/wynxo-cli/main/rm.ps1 | iex
~~~

## Troubleshooting

Run /doctor inside Wynxo. It checks the endpoint, selected model, tool support,
thinking support, context and GPU placement. When it can calculate a useful
context-window change, it prints the exact /context window command to try.

Use /model to see installed capabilities and observations from this machine.
Wynxo reports those observations; it does not silently choose another model.
