// proc_loopback: capture the audio rendered by one process tree with WASAPI process loopback and write raw PCM16 mono to stdout.
// Usage: proc_loopback.exe --pid N [--rate 16000]
// Build: see build.ps1 next to this file (clang against the MSVC toolchain and Windows SDK 10.0.22621 or newer).
#include <windows.h>
#include <audioclient.h>
#include <audioclientactivationparams.h>
#include <mmdeviceapi.h>
#include <propidl.h>
#include <fcntl.h>
#include <io.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

namespace {

struct Handler final : IActivateAudioInterfaceCompletionHandler, IAgileObject {
    LONG refs = 1;
    HANDLE done = nullptr;
    HRESULT activate_hr = E_FAIL;
    IAudioClient* client = nullptr;

    STDMETHODIMP QueryInterface(REFIID riid, void** out) override {
        if (!out) return E_POINTER;
        if (riid == __uuidof(IUnknown) || riid == __uuidof(IActivateAudioInterfaceCompletionHandler)) {
            *out = static_cast<IActivateAudioInterfaceCompletionHandler*>(this);
        } else if (riid == __uuidof(IAgileObject)) {
            *out = static_cast<IAgileObject*>(this);
        } else {
            *out = nullptr;
            return E_NOINTERFACE;
        }
        AddRef();
        return S_OK;
    }
    STDMETHODIMP_(ULONG) AddRef() override { return InterlockedIncrement(&refs); }
    STDMETHODIMP_(ULONG) Release() override {
        ULONG r = InterlockedDecrement(&refs);
        if (r == 0) delete this;
        return r;
    }
    STDMETHODIMP ActivateCompleted(IActivateAudioInterfaceAsyncOperation* op) override {
        HRESULT hr = S_OK;
        IUnknown* unk = nullptr;
        HRESULT r = op->GetActivateResult(&hr, &unk);
        if (SUCCEEDED(r) && SUCCEEDED(hr) && unk) {
            hr = unk->QueryInterface(__uuidof(IAudioClient), reinterpret_cast<void**>(&client));
        } else if (SUCCEEDED(r)) {
            r = hr;
        }
        if (unk) unk->Release();
        activate_hr = SUCCEEDED(r) ? hr : r;
        SetEvent(done);
        return S_OK;
    }
};

void die(const char* what, HRESULT hr) {
    std::fprintf(stderr, "proc_loopback: %s failed hr=0x%08lx\n", what, static_cast<unsigned long>(hr));
    std::exit(2);
}

bool write_all(HANDLE out, const void* data, DWORD len) {
    const BYTE* p = static_cast<const BYTE*>(data);
    while (len) {
        DWORD n = 0;
        if (!WriteFile(out, p, len, &n, nullptr)) return false;
        p += n;
        len -= n;
    }
    return true;
}

// Downmix to mono and resample by linear interpolation. Used only when the engine refuses the requested format.
struct Resampler {
    int in_rate = 48000, in_ch = 2, out_rate = 16000;
    double pos = 0.0;
    std::vector<float> carry;
    void process(const int16_t* in, size_t frames, std::vector<int16_t>& out) {
        std::vector<float> mono;
        mono.reserve(carry.size() + frames);
        mono.insert(mono.end(), carry.begin(), carry.end());
        for (size_t f = 0; f < frames; f++) {
            float acc = 0.f;
            for (int c = 0; c < in_ch; c++) acc += in[f * in_ch + c];
            mono.push_back(acc / in_ch);
        }
        const double step = static_cast<double>(in_rate) / out_rate;
        while (pos + 1.0 < mono.size()) {
            size_t i = static_cast<size_t>(pos);
            double frac = pos - i;
            float v = static_cast<float>(mono[i] * (1.0 - frac) + mono[i + 1] * frac);
            out.push_back(static_cast<int16_t>(v));
            pos += step;
        }
        size_t keep = static_cast<size_t>(pos);
        if (keep > mono.size()) keep = mono.size();
        carry.assign(mono.begin() + keep, mono.end());
        pos -= keep;
    }
};

}  // namespace

int main(int argc, char** argv) {
    DWORD pid = 0;
    int rate = 16000;
    for (int i = 1; i < argc; i++) {
        if (!std::strcmp(argv[i], "--pid") && i + 1 < argc) pid = std::strtoul(argv[++i], nullptr, 10);
        else if (!std::strcmp(argv[i], "--rate") && i + 1 < argc) rate = std::atoi(argv[++i]);
    }
    if (!pid) {
        std::fprintf(stderr, "usage: proc_loopback --pid N [--rate 16000]\n");
        return 1;
    }
    _setmode(_fileno(stdout), _O_BINARY);
    HANDLE out = GetStdHandle(STD_OUTPUT_HANDLE);
    HANDLE target = OpenProcess(SYNCHRONIZE, FALSE, pid);

    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(hr)) die("CoInitializeEx", hr);

    AUDIOCLIENT_ACTIVATION_PARAMS params = {};
    params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK;
    params.ProcessLoopbackParams.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE;
    params.ProcessLoopbackParams.TargetProcessId = pid;
    PROPVARIANT pv = {};
    pv.vt = VT_BLOB;
    pv.blob.cbSize = sizeof(params);
    pv.blob.pBlobData = reinterpret_cast<BYTE*>(&params);

    Handler* h = new Handler();
    h->done = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    IActivateAudioInterfaceAsyncOperation* op = nullptr;
    hr = ActivateAudioInterfaceAsync(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, __uuidof(IAudioClient), &pv, h, &op);
    if (FAILED(hr)) die("ActivateAudioInterfaceAsync", hr);
    WaitForSingleObject(h->done, INFINITE);
    if (op) op->Release();
    if (FAILED(h->activate_hr) || !h->client) die("activation", h->activate_hr);
    IAudioClient* client = h->client;

    WAVEFORMATEX wfx = {};
    wfx.wFormatTag = WAVE_FORMAT_PCM;
    wfx.nChannels = 1;
    wfx.nSamplesPerSec = rate;
    wfx.wBitsPerSample = 16;
    wfx.nBlockAlign = wfx.nChannels * wfx.wBitsPerSample / 8;
    wfx.nAvgBytesPerSec = wfx.nSamplesPerSec * wfx.nBlockAlign;
    const DWORD flags = AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY;
    bool native = true;
    hr = client->Initialize(AUDCLNT_SHAREMODE_SHARED, flags, 0, 0, &wfx, nullptr);
    if (FAILED(hr)) {
        // Fall back to the format the Microsoft sample uses and convert ourselves.
        native = false;
        wfx.nChannels = 2;
        wfx.nSamplesPerSec = 48000;
        wfx.nBlockAlign = wfx.nChannels * wfx.wBitsPerSample / 8;
        wfx.nAvgBytesPerSec = wfx.nSamplesPerSec * wfx.nBlockAlign;
        hr = client->Initialize(AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK, 0, 0, &wfx, nullptr);
        if (FAILED(hr)) die("IAudioClient::Initialize", hr);
    }
    std::fprintf(stderr, "proc_loopback: pid=%lu format=%s %lu Hz %u ch\n", static_cast<unsigned long>(pid), native ? "native" : "fallback", static_cast<unsigned long>(wfx.nSamplesPerSec), static_cast<unsigned>(wfx.nChannels));

    IAudioCaptureClient* capture = nullptr;
    hr = client->GetService(__uuidof(IAudioCaptureClient), reinterpret_cast<void**>(&capture));
    if (FAILED(hr)) die("GetService(IAudioCaptureClient)", hr);
    HANDLE ev = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    hr = client->SetEventHandle(ev);
    if (FAILED(hr)) die("SetEventHandle", hr);
    hr = client->Start();
    if (FAILED(hr)) die("Start", hr);

    Resampler rs;
    rs.in_rate = wfx.nSamplesPerSec;
    rs.in_ch = wfx.nChannels;
    rs.out_rate = rate;
    std::vector<int16_t> conv;
    std::vector<int16_t> zeros;

    for (;;) {
        HANDLE waits[2] = {ev, target};
        DWORD w = WaitForMultipleObjects(target ? 2 : 1, waits, FALSE, 200);
        if (w == WAIT_OBJECT_0 + 1) break;  // target process exited
        UINT32 packet = 0;
        while (SUCCEEDED(capture->GetNextPacketSize(&packet)) && packet) {
            BYTE* data = nullptr;
            UINT32 frames = 0;
            DWORD bflags = 0;
            hr = capture->GetBuffer(&data, &frames, &bflags, nullptr, nullptr);
            if (FAILED(hr)) die("GetBuffer", hr);
            const size_t bytes = static_cast<size_t>(frames) * wfx.nBlockAlign;
            const int16_t* pcm = reinterpret_cast<const int16_t*>(data);
            if (bflags & AUDCLNT_BUFFERFLAGS_SILENT) {
                zeros.assign(bytes / 2, 0);
                pcm = zeros.data();
            }
            bool ok;
            if (native) {
                ok = write_all(out, pcm, static_cast<DWORD>(bytes));
            } else {
                conv.clear();
                rs.process(pcm, frames, conv);
                ok = write_all(out, conv.data(), static_cast<DWORD>(conv.size() * 2));
            }
            capture->ReleaseBuffer(frames);
            if (!ok) goto done;  // stdout closed by the parent
        }
    }
done:
    client->Stop();
    capture->Release();
    client->Release();
    h->Release();
    CoUninitialize();
    return 0;
}
