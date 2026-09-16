# Firefox real time caption & translation

An extension for firefox and a backend server (windows only) for compute to generate real time captions in English for videos playing in browser.

- Can be activated per tab.
- Auto detects active video playing audio
- Overlays the caption
- Caption UI can be customised and auto fits in the video layout as firefox is resized. 
- Auto detects language in real time.
- AI models only activates if extension is turned on for any tab and has active data streaming leading to good use of compute resources when not in use.
- Server resides in windows tray (WIP)

## Installation
1. Download the backend installation files (.exe and .bin files, so 4 files in total) from the release section. [Firefox Add Ons: Live Caption Video](https://addons.mozilla.org/en-GB/firefox/addon/live-caption-video/) 
2. Make sure these files are placed in the same folder. Then run the `LiveCaptionTranslate-Setup-x.x.x.exe` to install the backend. Once installed, you can delete the downloaded files.
3. Install the extension from the firefox add on page here [Releases](https://github.com/revoconner/Firefox-live-caption-translation/releases). 
    - If you are using the Firefox Developer Edition, you can also download the extension built file and install it manually. The one in Release section is unsigned, however.
4. Restart browser (recommended).

### Important note
- CUDA GPU needed. CUDA 13 supported GPU recommended. 
- The extension without the backend won't work since an extension (as far as I know) cannot run CUDA accelarated AI models for processing text streams. 
- This also means that the extension will not work on incompatible platform like linux, or macOS, although feel free to build your own backend from source. 
- For a rough idea of build instructions, read the CLAUDE.md but instructions to build from source will not be provided.

### Uninstall
To uninstall uninstall the backend package and remove the extension.

## Languages supported
### Great coverage
- English (en-US, en-GB)
- Spanish (es-US, es-ES)
- French (fr-FR, fr-CA)
- Italian (it-IT)
- Portuguese (pt-BR, pt-PT)
- Dutch (nl-NL)
- German (de-DE)
- Turkish (tr-TR)
- Russian (ru-RU)
- Arabic (ar-AR)
- Hindi (hi-IN)
- Japanese (ja-JP)
- Korean (ko-KR)
- Vietnamese (vi-VN)
- Ukrainian (uk-UA)

### Not so great coverage
- Polish (pl-PL)
- Swedish (sv-SE)
- Czech (cs-CZ)
- Norwegian Bokmål (nb-NO)
- Danish (da-DK)
- Bulgarian (bg-BG)
- Finnish (fi-FI)
- Croatian (hr-HR)
- Slovak (sk-SK)
- Mandarin (zh-CN)
- Hungarian (hu-HU)
- Romanian (ro-RO)
- Estonian (et-EE)

## Compute resources used by the backend

| state | private RAM | working set | VRAM | CPU cores
| ---- | ---- | ---- | ---- | ---- |
|   idle, models loaded   |   236 MB   |   4.7 GB   |   5.0 GB    |   0.19   |
|   transcribing and translating   |   350 MB   |  4.8 GB    |    5.9 GB   |  0.29   |

**The target machine for this specific tool is my workstation PC which is specced as written below**:
- **GPU**: RTX 4090 24GB Vram (driver 616.56)
- **CPU**: Threadripper 7960x 24 cores.
- **Memory**: 128GB RDIMM DDR5 4800MT/s
- **OS**: Windows Enterprise LTSC build 26100 (24H2)

On the target machine (or similar) compute resources is unnoticeable even in full compute. Your mileage may vary.

_The data was derived via exclusion method of running different stages of the tool, then subtracting background noise so its not 100% accurate._


## AI Disclaimer
This project has been co-authored by Claude Fable 5.1 by Anthropic <noreply@anthropic.com>.

----

**License Scope and clarification**

- The final binary includes transformer models from several companies. The license for this repository does not apply to the model files and the models themselves are not included in the repository.
- The icon for the binary and extension are part of [svgrepo.com](svgrepo.com) repository and the license for this repository does not apply to them. The files are included in the repository.
