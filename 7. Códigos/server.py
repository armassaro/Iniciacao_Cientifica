"""
server.py - Receptor VLP (Visible Light Positioning) por RSS
============================================================

Recebe do ESP32 (SoftAP + TCP, 192.168.4.1:5000) 1 pacote/s com N_CH canais
ADC intercalados. Cada canal e a intensidade luminosa recebida de um LED
ancora com posicao conhecida -> RSS (Received Signal Strength).

Abre DUAS janelas:
  (1) Sinais ADC discretos por canal (diagnostico do hardware).
  (2) Mapa de localizacao VLP: ancoras (LEDs), forca do sinal e a posicao
      estimada, com um seletor para alternar entre os algoritmos.

>>> ONDE VOCE VAI TRABALHAR:
    - Tabela ANCHORS: posicao de cada LED e qual canal ADC o mede.
    - rss_from_samples(): como extrair a RSS de cada canal.
    - rss_to_distance(): modelo RSS->distancia (precisa de calibracao).
    - ALGORITHMS: registre aqui seus 3 algoritmos de localizacao.

Protocolo (little-endian): [uint32 N_FRAMES][uint32 N_CH][uint16 intercalado]
"""

import socket
import struct
import time
import threading
from datetime import datetime

try:
    import numpy as np
    import matplotlib.pyplot as plt
    import matplotlib.animation as animation
    from matplotlib.widgets import RadioButtons
    from matplotlib.patches import Circle
    PLOT_ENABLED = True
except ImportError:
    PLOT_ENABLED = False
    print("[AVISO] numpy/matplotlib nao instalados - janelas desativadas.")

# ── Rede ────────────────────────────────────────────────────────────────────
ESP32_IP   = "192.168.4.1"
ESP32_PORT = 5000
ADC_MAX    = 4095
V_REF      = 3.9
RX_TIMEOUT = 6
PLOT_WINDOW = 120            # amostras discretas exibidas por canal (janela 1)

# ════════════════════════════════════════════════════════════════════════════
#  CONFIGURACAO VLP  (edite conforme seu experimento)
# ════════════════════════════════════════════════════════════════════════════
#  CONFIGURACAO VLP  (edite conforme seu experimento)
#
#  Modelo: os LEDs sao ANCORAS (posicao conhecida). O RECEPTOR e o alvo cuja
#  posicao os algoritmos estimam. Voce coloca o receptor em pontos conhecidos
#  (RECEIVER_TRUE_POS) para medir o erro de cada algoritmo.
# ════════════════════════════════════════════════════════════════════════════

AREA_W, AREA_H = 2.0, 2.0     # dimensoes do ambiente (m), plano X-Y do teto

SAMPLE_RATE = 5000            # Hz - deve bater com FRAME_RATE_HZ do firmware

# Como separar a RSS de cada LED a partir do sinal do fotodiodo:
#   "fft"         -> LEDs modulados em frequencias distintas; separa por FFT.
#                    (1 fotodiodo capta todos; use o campo "freq" das ancoras)
#   "per_channel" -> cada LED ja chega isolado num canal ADC proprio
#                    (use o campo "ch" das ancoras)
RSS_MODE     = "fft"
PHOTODIODE_CH = 0             # (modo fft) qual canal ADC e o fotodiodo receptor

# Ancoras = LEDs. pos: posicao conhecida (m). freq: frequencia de modulacao
# (Hz, modo fft). ch: canal ADC (modo per_channel).
ANCHORS = [
    {"id": "LED0", "pos": (0.0,    0.0),    "freq": 500,  "ch": 0},
    {"id": "LED1", "pos": (AREA_W, 0.0),    "freq": 1000, "ch": 1},
    {"id": "LED2", "pos": (AREA_W, AREA_H), "freq": 1500, "ch": 2},
    {"id": "LED3", "pos": (0.0,    AREA_H), "freq": 2000, "ch": 3},
]

# Posicao REAL do receptor no ponto de teste (ground truth) para medir erro.
# Defina como (x, y) ao validar; deixe None para desativar.
RECEIVER_TRUE_POS = None      # ex.: (1.0, 0.8)

# Modelo RSS -> distancia (calibrar!):  d = REF_D * (REF_RSS/rss)**(1/N)
REF_RSS, REF_D, PATHLOSS_N = 3000.0, 1.0, 2.0


def rss_from_samples(series) -> float:
    """RSS de um canal ja isolado (modo per_channel): amplitude AC (RMS)."""
    if series is None or len(series) == 0:
        return 0.0
    arr = np.asarray(series, dtype=float)
    return float(np.sqrt(np.mean((arr - arr.mean()) ** 2)))


def extract_rss(channels) -> dict:
    """Retorna {led_id: rss} a partir dos dados brutos dos canais ADC."""
    if not channels:
        return {}
    if RSS_MODE == "per_channel":
        return {a["id"]: rss_from_samples(channels[a["ch"]])
                for a in ANCHORS if a["ch"] < len(channels)}
    # modo "fft": separa cada LED pela sua frequencia de modulacao
    if PHOTODIODE_CH >= len(channels):
        return {}
    sig = np.asarray(channels[PHOTODIODE_CH], dtype=float)
    sig = sig - sig.mean()
    n = len(sig)
    mag = np.abs(np.fft.rfft(sig)) * (2.0 / n)
    freqs = np.fft.rfftfreq(n, d=1.0 / SAMPLE_RATE)
    rss = {}
    for a in ANCHORS:
        k = int(np.argmin(np.abs(freqs - a["freq"])))
        rss[a["id"]] = float(mag[k])
    return rss


def rss_to_distance(rss: float):
    if rss <= 0:
        return None
    return REF_D * (REF_RSS / rss) ** (1.0 / PATHLOSS_N)


# ════════════════════════════════════════════════════════════════════════════
#  ALGORITMOS DE LOCALIZACAO   f(anchors, rss) -> (x, y) | None
#    anchors: [{id, pos:(x,y), ...}]   rss: {led_id: valor}
# ════════════════════════════════════════════════════════════════════════════

def weighted_centroid(anchors, rss):
    """[FUNCIONAL] Centroide ponderado pela RSS. Baseline sem calibracao."""
    nx = ny = den = 0.0
    for a in anchors:
        w = max(rss.get(a["id"], 0.0), 0.0)
        nx += w * a["pos"][0]; ny += w * a["pos"][1]; den += w
    return (nx / den, ny / den) if den > 0 else None


def trilateration_lsq(anchors, rss):
    """[FUNCIONAL apos calibrar] Trilateracao por minimos quadrados."""
    pts, ds = [], []
    for a in anchors:
        d = rss_to_distance(rss.get(a["id"], 0.0))
        if d is not None:
            pts.append(a["pos"]); ds.append(d)
    if len(pts) < 3:
        return None
    x = np.array([p[0] for p in pts]); y = np.array([p[1] for p in pts])
    d = np.array(ds)
    A = np.column_stack([2 * (x[:-1] - x[-1]), 2 * (y[:-1] - y[-1])])
    b = (x[:-1]**2 - x[-1]**2) + (y[:-1]**2 - y[-1]**2) + (d[-1]**2 - d[:-1]**2)
    try:
        sol, *_ = np.linalg.lstsq(A, b, rcond=None)
        return (float(sol[0]), float(sol[1]))
    except np.linalg.LinAlgError:
        return None


def fingerprint_knn(anchors, rss):
    """[STUB - IMPLEMENTE] KNN sobre base coletada offline (pos -> vetor RSS)."""
    return None


ALGORITHMS = {
    "1 - Centroide Ponderado": weighted_centroid,
    "2 - Trilateracao (LSQ)":  trilateration_lsq,
    "3 - Fingerprint (stub)":  fingerprint_knn,
}

latest_channels: list = []
latest_lock = threading.Lock()
packet_count = 0
selected_algo = next(iter(ALGORITHMS))
_keep_alive: list = []


# ════════════════════════════════════════════════════════════════════════════

def ip_local_para(destino: str):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((destino, 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def checar_rede() -> None:
    ip = ip_local_para(ESP32_IP)
    if ip is None:
        print("[DIAG] Sem rota para 192.168.4.1. PC nao esta no Wi-Fi ESP32_ADC.")
    elif not ip.startswith("192.168.4."):
        print(f"[DIAG] IP local = {ip} (esperado 192.168.4.x). PC em outra rede.")
    else:
        print(f"[DIAG] IP local = {ip} -> PC na rede do ESP32. OK.")


# ════════════════════════════════════════════════════════════════════════════
#  RECEPCAO TCP
# ════════════════════════════════════════════════════════════════════════════

def recv_exact(conn: socket.socket, n_bytes: int) -> bytes:
    buf = bytearray()
    while len(buf) < n_bytes:
        chunk = conn.recv(n_bytes - len(buf))
        if not chunk:
            raise ConnectionResetError("Conexao encerrada pelo ESP32.")
        buf.extend(chunk)
    return bytes(buf)


def stream(conn: socket.socket) -> None:
    global packet_count
    conn.settimeout(RX_TIMEOUT)
    primeiro = True
    while True:
        try:
            n_frames, n_ch = struct.unpack("<II", recv_exact(conn, 8))
        except socket.timeout:
            if primeiro:
                print(f"[DIAG] Conectado, mas o ESP32 nao enviou nada em "
                      f"{RX_TIMEOUT}s. Veja o Serial Monitor.")
            raise

        if primeiro:
            print("[OK] Streaming iniciado.\n")
            primeiro = False

        if not (0 < n_frames <= 200_000) or not (0 < n_ch <= 16):
            print(f"  [ERRO] cabecalho invalido: {n_frames}/{n_ch}. Abortando.")
            break

        total = n_frames * n_ch
        flat = struct.unpack(f"<{total}H", recv_exact(conn, total * 2))
        channels = [flat[c::n_ch] for c in range(n_ch)]
        packet_count += 1

        with latest_lock:
            latest_channels.clear()
            latest_channels.extend(list(s) for s in channels)

        rss = extract_rss(channels)
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] Pacote #{packet_count:04d} | {n_ch}x{n_frames} | RSS(LED): " +
              " ".join(f"{k}={v:.0f}" for k, v in rss.items()))


def connect_with_retry() -> socket.socket:
    while True:
        checar_rede()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(8)
            s.connect((ESP32_IP, ESP32_PORT))
            print(f"[CLIENTE] Conectado em {ESP32_IP}:{ESP32_PORT}")
            return s
        except (socket.timeout, OSError) as e:
            print(f"[CLIENTE] Nao conectou ({e}). Nova tentativa em 3 s...\n")
            time.sleep(3)


def client_loop() -> None:
    while True:
        conn = connect_with_retry()
        try:
            stream(conn)
        except (ConnectionResetError, socket.timeout, struct.error, OSError) as e:
            print(f"[CLIENTE] Conexao perdida: {e}. Reconectando...\n")
        finally:
            try:
                conn.close()
            except OSError:
                pass
        time.sleep(1)


# ════════════════════════════════════════════════════════════════════════════
#  JANELA 1 - SINAIS ADC DISCRETOS
# ════════════════════════════════════════════════════════════════════════════

def build_signal_window():
    palette = ["#00c8ff", "#ff6b35", "#7bd16b", "#d16bd1",
               "#ffd23f", "#ff5c8a", "#5c9cff", "#9c5cff"]
    fig, ax = plt.subplots(figsize=(11, 5), num="VLP - Sinais ADC")
    fig.suptitle(f"Amostras ADC (discreto) - primeiras {PLOT_WINDOW} por canal",
                 fontsize=12)
    ax.set_xlabel("Indice da amostra (n)"); ax.set_ylabel("ADC (counts)")
    ax.set_xlim(-1, PLOT_WINDOW); ax.set_ylim(-100, ADC_MAX + 100)
    ax.grid(True, alpha=0.3)
    markers, stems = [], []

    def update(_):
        with latest_lock:
            data = [list(s)[:PLOT_WINDOW] for s in latest_channels]
        if not data:
            return markers
        n_ch = len(data)
        if len(markers) != n_ch:
            ax.cla()
            ax.set_xlabel("Indice da amostra (n)"); ax.set_ylabel("ADC (counts)")
            ax.set_xlim(-1, PLOT_WINDOW); ax.set_ylim(-100, ADC_MAX + 100)
            ax.grid(True, alpha=0.3)
            markers.clear(); stems.clear()
            for c in range(n_ch):
                cor = palette[c % len(palette)]
                (mk,) = ax.plot([], [], linestyle="None", marker="o",
                                markersize=4, color=cor, label=f"ch{c}")
                stems.append(ax.vlines([], [], [], color=cor, lw=0.6, alpha=0.5))
                markers.append(mk)
            ax.legend(loc="upper right", ncol=min(n_ch, 4), fontsize=8)
        for c in range(n_ch):
            y = data[c]; x = list(range(len(y)))
            markers[c].set_data(x, y)
            stems[c].set_segments([[(xi, 0), (xi, yi)] for xi, yi in zip(x, y)])
        return markers

    ani = animation.FuncAnimation(fig, update, interval=1100,
                                  blit=False, cache_frame_data=False)
    return fig, ani


# ════════════════════════════════════════════════════════════════════════════
#  JANELA 2 - MAPA DE LOCALIZACAO VLP
# ════════════════════════════════════════════════════════════════════════════

def build_localization_window():
    fig = plt.figure(figsize=(10, 7), num="VLP - Localizacao")
    ax = fig.add_axes([0.32, 0.08, 0.64, 0.86])
    ax_radio = fig.add_axes([0.02, 0.60, 0.26, 0.28])

    m = 0.3
    ax.set_xlim(-m, AREA_W + m); ax.set_ylim(-m, AREA_H + m)
    ax.set_aspect("equal"); ax.grid(True, alpha=0.3)
    ax.set_xlabel("X (m)"); ax.set_ylabel("Y (m)")
    ax.set_title("Localizacao por luz visivel (RSS)")
    ax.add_patch(plt.Rectangle((0, 0), AREA_W, AREA_H, fill=False, ls="--", ec="#888"))

    # LEDs ancora
    ap = np.array([a["pos"] for a in ANCHORS])
    anchor_sc = ax.scatter(ap[:, 0], ap[:, 1], s=140, marker="*", c="#f2b705",
                           edgecolors="#7a5c00", zorder=5, label="LEDs (ancoras)")
    for a in ANCHORS:
        ax.annotate(a["id"], a["pos"], textcoords="offset points",
                    xytext=(6, 6), fontsize=8, color="#555")

    # Circulos de distancia (trilateracao)
    from matplotlib.patches import Circle
    circles = []
    for a in ANCHORS:
        c = Circle(a["pos"], 0.0, fill=False, ls=":", ec="#4aa3ff", alpha=0.3, zorder=2)
        ax.add_patch(c); circles.append(c)

    # Ground truth (posicao real do receptor) e estimativa
    (truth_mk,) = ax.plot([], [], marker="o", ms=12, mfc="none", mec="#2a9d8f",
                          mew=2, zorder=6, label="Receptor (real)")
    (est_mk,) = ax.plot([], [], marker="X", ms=16, color="#e63946",
                        mec="black", zorder=7, label="Posicao estimada")
    info = ax.text(0.02, 0.98, "", transform=ax.transAxes, va="top", fontsize=8,
                   family="monospace", bbox=dict(boxstyle="round", fc="white", alpha=0.85))
    ax.legend(loc="lower right", fontsize=8)

    radio = RadioButtons(ax_radio, list(ALGORITHMS.keys()))
    ax_radio.set_title("Algoritmo (visualizado)", fontsize=9)
    def on_select(label):
        global selected_algo
        selected_algo = label
    radio.on_clicked(on_select)
    _keep_alive.append(radio)

    def update(_):
        with latest_lock:
            data = [list(s) for s in latest_channels]
        rss = extract_rss(data)

        if rss:
            mx = max(rss.values()) or 1.0
            anchor_sc.set_sizes([140 + 420 * (rss.get(a["id"], 0.0) / mx) for a in ANCHORS])
        for c, a in zip(circles, ANCHORS):
            d = rss_to_distance(rss.get(a["id"], 0.0)); c.set_radius(d or 0.0)

        # Roda TODOS os algoritmos (para comparar), destaca o selecionado
        results = {}
        for name, fn in ALGORITHMS.items():
            try:
                results[name] = fn(ANCHORS, rss) if rss else None
            except Exception as e:
                results[name] = None
                print(f"[ALGO] erro em {name}: {e}")

        est = results.get(selected_algo)
        if est is not None:
            est_mk.set_data([est[0]], [est[1]])
        else:
            est_mk.set_data([], [])

        if RECEIVER_TRUE_POS is not None:
            truth_mk.set_data([RECEIVER_TRUE_POS[0]], [RECEIVER_TRUE_POS[1]])
        else:
            truth_mk.set_data([], [])

        linhas = [f"Selecionado: {selected_algo}", ""]
        for name, r in results.items():
            if r is None:
                linhas.append(f"{name[:16]:16s}: --")
            else:
                err = ""
                if RECEIVER_TRUE_POS is not None:
                    e = ((r[0]-RECEIVER_TRUE_POS[0])**2 + (r[1]-RECEIVER_TRUE_POS[1])**2)**0.5
                    err = f"  erro={e:.2f}m"
                linhas.append(f"{name[:16]:16s}: ({r[0]:+.2f},{r[1]:+.2f}){err}")
        linhas.append("")
        for a in ANCHORS:
            linhas.append(f"{a[id]}: RSS={rss.get(a[id], 0.0):6.0f}")
        info.set_text("\n".join(linhas))
        return est_mk, truth_mk

    ani = animation.FuncAnimation(fig, update, interval=1100, blit=False,
                                  cache_frame_data=False)
    return fig, ani


# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  Receptor VLP por RSS - ESP32")
    print(f"  Alvo: {ESP32_IP}:{ESP32_PORT} | {len(ANCHORS)} ancoras")
    print("=" * 60)

    threading.Thread(target=client_loop, daemon=True).start()

    if PLOT_ENABLED:
        fig1, ani1 = build_signal_window()
        fig2, ani2 = build_localization_window()
        _keep_alive += [ani1, ani2]
        plt.show()
    else:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nEncerrado.")
