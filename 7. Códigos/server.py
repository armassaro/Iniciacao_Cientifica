"""
server.py – Servidor TCP para receber amostras ADC do ESP32
============================================================

Protocolo (binário, little-endian):
  [4 bytes] uint32  → número de amostras N
  [N×4 bytes] int32 → vetor de amostras ADC

Uso:
  pip install numpy matplotlib
  python server.py

O servidor fica escutando na porta 5000 e, a cada segundo,
recebe um vetor de 10.000 amostras, exibe estatísticas e
(opcionalmente) plota o sinal em tempo real.
"""

import socket
import struct
import time
import threading
from datetime import datetime

# ── Opcional: matplotlib para plot ao vivo ──────────────────────────────────
try:
    import numpy as np
    import matplotlib.pyplot as plt
    import matplotlib.animation as animation
    PLOT_ENABLED = True
except ImportError:
    PLOT_ENABLED = False
    print("[AVISO] numpy/matplotlib não instalados – plot desativado.")
    print("        Execute: pip install numpy matplotlib")

# ── Configurações ────────────────────────────────────────────────────────────
HOST        = "0.0.0.0"    # escuta em todas as interfaces
PORT        = 5000
SAMPLE_RATE = 10_000       # Hz (deve bater com o firmware)
ADC_MAX     = 4095         # resolução 12 bits
V_REF       = 3.9          # tensão de referência (ADC_ATTEN_DB_11 ≈ 0–3.9 V)

# Buffer compartilhado entre a thread de recepção e o plot
latest_samples: list[int] = []
latest_lock = threading.Lock()
packet_count = 0


# ════════════════════════════════════════════════════════════════════════════
#  RECEPÇÃO TCP
# ════════════════════════════════════════════════════════════════════════════

def recv_exact(conn: socket.socket, n_bytes: int) -> bytes:
    """Recebe exatamente n_bytes do socket (garante fragmentação TCP)."""
    buf = b""
    while len(buf) < n_bytes:
        chunk = conn.recv(n_bytes - len(buf))
        if not chunk:
            raise ConnectionResetError("Conexão encerrada pelo ESP32.")
        buf += chunk
    return buf


def handle_client(conn: socket.socket, addr: tuple) -> None:
    """Processa uma conexão: lê cabeçalho + payload, atualiza buffer global."""
    global packet_count
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{ts}] Conexão de {addr[0]}:{addr[1]}")

    try:
        # ── Cabeçalho: número de amostras (uint32, 4 bytes) ────────────────
        hdr = recv_exact(conn, 4)
        (n_samples,) = struct.unpack("<I", hdr)

        if n_samples == 0 or n_samples > 100_000:
            print(f"  [ERRO] n_samples inválido: {n_samples}. Pacote descartado.")
            return

        # ── Payload: n_samples × int32 ─────────────────────────────────────
        payload = recv_exact(conn, n_samples * 4)
        samples = list(struct.unpack(f"<{n_samples}i", payload))

        packet_count += 1

        # ── Estatísticas ────────────────────────────────────────────────────
        mn  = min(samples)
        mx  = max(samples)
        avg = sum(samples) / len(samples)
        v_avg = avg / ADC_MAX * V_REF
        v_mn  = mn  / ADC_MAX * V_REF
        v_mx  = mx  / ADC_MAX * V_REF

        print(f"  Pacote #{packet_count:04d} | {n_samples} amostras | "
              f"ADC min={mn} max={mx} avg={avg:.1f}")
        print(f"  Tensão   | min={v_mn:.3f}V max={v_mx:.3f}V avg={v_avg:.3f}V")

        # ── Atualiza buffer global para o plot ─────────────────────────────
        with latest_lock:
            latest_samples.clear()
            latest_samples.extend(samples)

        # ── Salva CSV (opcional – descomente se quiser persistir) ──────────
        # save_csv(samples, packet_count)

    except (ConnectionResetError, struct.error) as e:
        print(f"  [ERRO] {e}")
    finally:
        conn.close()


def save_csv(samples: list[int], idx: int) -> None:
    """Salva amostras em arquivo CSV."""
    fname = f"adc_pacote_{idx:04d}.csv"
    with open(fname, "w") as f:
        f.write("amostra,valor_adc,tensao_v\n")
        for i, v in enumerate(samples):
            f.write(f"{i},{v},{v / ADC_MAX * V_REF:.4f}\n")
    print(f"  Salvo: {fname}")


def server_loop() -> None:
    """Loop principal do servidor TCP."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((HOST, PORT))
        srv.listen(5)
        print(f"[SERVER] Escutando em {HOST}:{PORT}")
        print(f"[SERVER] Aguardando ESP32... (Ctrl+C para parar)\n")

        while True:
            try:
                conn, addr = srv.accept()
                # Cada conexão em thread separada (não bloqueia o accept)
                t = threading.Thread(target=handle_client, args=(conn, addr),
                                     daemon=True)
                t.start()
            except KeyboardInterrupt:
                print("\n[SERVER] Encerrando.")
                break


# ════════════════════════════════════════════════════════════════════════════
#  PLOT AO VIVO (matplotlib)
# ════════════════════════════════════════════════════════════════════════════

def start_plot() -> None:
    if not PLOT_ENABLED:
        return

    fig, (ax_time, ax_freq) = plt.subplots(2, 1, figsize=(10, 6))
    fig.suptitle("ESP32 – Sinal ADC em Tempo Real (10 kHz)", fontsize=13)

    t_axis = np.linspace(0, 1, SAMPLE_RATE)
    line_t,  = ax_time.plot(t_axis, np.zeros(SAMPLE_RATE), color="#00c8ff", lw=0.8)
    line_f,  = ax_freq.plot([], [], color="#ff6b35", lw=0.8)

    ax_time.set_xlim(0, 1)
    ax_time.set_ylim(-100, ADC_MAX + 100)
    ax_time.set_xlabel("Tempo (s)")
    ax_time.set_ylabel("ADC (counts)")
    ax_time.set_title("Domínio do Tempo")
    ax_time.grid(True, alpha=0.3)

    ax_freq.set_xlim(0, SAMPLE_RATE / 2)
    ax_freq.set_ylim(0, 1)
    ax_freq.set_xlabel("Frequência (Hz)")
    ax_freq.set_ylabel("|FFT| normalizado")
    ax_freq.set_title("Espectro de Frequência (FFT)")
    ax_freq.grid(True, alpha=0.3)

    def update(_frame):
        with latest_lock:
            if len(latest_samples) != SAMPLE_RATE:
                return line_t, line_f

            data = np.array(latest_samples, dtype=np.float32)

        # Domínio do tempo
        line_t.set_ydata(data)

        # FFT
        fft_vals = np.abs(np.fft.rfft(data)) / SAMPLE_RATE
        fft_norm = fft_vals / (fft_vals.max() + 1e-9)
        freqs    = np.fft.rfftfreq(SAMPLE_RATE, d=1.0 / SAMPLE_RATE)
        line_f.set_data(freqs, fft_norm)

        return line_t, line_f

    ani = animation.FuncAnimation(fig, update, interval=1100,
                                  blit=True, cache_frame_data=False)
    plt.tight_layout()
    plt.show()


# ════════════════════════════════════════════════════════════════════════════
#  PONTO DE ENTRADA
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 55)
    print("  ESP32 ADC Stream – Servidor TCP")
    print(f"  Taxa: {SAMPLE_RATE} Hz | Resolução: 12 bits | Vref: {V_REF}V")
    print("=" * 55)

    # Servidor em thread de background
    srv_thread = threading.Thread(target=server_loop, daemon=True)
    srv_thread.start()

    # Plot roda na thread principal (requisito do matplotlib)
    if PLOT_ENABLED:
        print("[PLOT] Abrindo visualização ao vivo...")
        start_plot()
    else:
        # Sem plot: fica vivo enquanto o servidor rodar
        try:
            srv_thread.join()
        except KeyboardInterrupt:
            print("\nEncerrado.")
