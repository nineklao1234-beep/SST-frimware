import time
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import serial
import serial.tools.list_ports

class FlashProgrammerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("SST26VF016B Tester & Programmer - ESP32")
        self.root.geometry("640x480")
        self.root.resizable(False, False)
        
        self.ser = None

        style = ttk.Style()
        style.theme_use('clam')

        # --- Top Connection Bar ---
        conn_frame = ttk.LabelFrame(root, text=" Serial Connection ")
        conn_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(conn_frame, text="Port:").grid(row=0, column=0, padx=5, pady=5)
        self.port_cb = ttk.Combobox(conn_frame, width=12, state="readonly")
        self.port_cb.grid(row=0, column=1, padx=5, pady=5)
        
        btn_refresh = ttk.Button(conn_frame, text="Refresh", command=self.refresh_ports)
        btn_refresh.grid(row=0, column=2, padx=5, pady=5)

        self.btn_connect = ttk.Button(conn_frame, text="Connect", command=self.toggle_connection)
        self.btn_connect.grid(row=0, column=3, padx=5, pady=5)

        self.led_canvas = tk.Canvas(conn_frame, width=20, height=20, highlightthickness=0)
        self.led_canvas.grid(row=0, column=4, padx=(15, 2), pady=5)
        self.led_circle = self.led_canvas.create_oval(3, 3, 17, 17, fill="#FF3B30", outline="#B30000")

        self.lbl_status = ttk.Label(conn_frame, text="Disconnected", font=("Segoe UI", 9, "bold"), foreground="#FF3B30")
        self.lbl_status.grid(row=0, column=5, padx=5, pady=5)

        # --- Tab System (Notebook) ---
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=5)

        # Tab 1: Status & Info
        self.tab_status = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_status, text=" 📊 Device Status ")
        self.setup_tab_status()

        # Tab 2: Memory Operations (ลบ / เขียน / อ่าน / ตรวจ)
        self.tab_ops = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_ops, text=" ⚡ Memory Operations ")
        self.setup_tab_ops()

        self.refresh_ports()

    def set_led_status(self, connected):
        if connected:
            self.led_canvas.itemconfig(self.led_circle, fill="#34C759", outline="#1D8339")
            self.lbl_status.config(text="Connected", foreground="#34C759")
            self.btn_connect.config(text="Disconnect")
        else:
            self.led_canvas.itemconfig(self.led_circle, fill="#FF3B30", outline="#B30000")
            self.lbl_status.config(text="Disconnected", foreground="#FF3B30")
            self.btn_connect.config(text="Connect")

    def setup_tab_status(self):
        dev_frame = ttk.LabelFrame(self.tab_status, text=" Device Control ")
        dev_frame.pack(fill="x", padx=10, pady=10)

        ttk.Button(dev_frame, text="Read JEDEC ID", command=self.cmd_read_id).grid(row=0, column=0, padx=10, pady=10)
        ttk.Button(dev_frame, text="Global Protection Unlock", command=self.cmd_unlock).grid(row=0, column=1, padx=10, pady=10)
        
        self.lbl_jedec = ttk.Label(dev_frame, text="JEDEC ID: -- -- --", font=("Consolas", 10, "bold"))
        self.lbl_jedec.grid(row=0, column=2, padx=15, pady=10)

        reg_frame = ttk.LabelFrame(self.tab_status, text=" Register Status ")
        reg_frame.pack(fill="x", padx=10, pady=5)

        ttk.Button(reg_frame, text="Read Registers", command=self.cmd_read_regs).grid(row=0, column=0, padx=10, pady=10)

        reg_info_frame = ttk.Frame(reg_frame)
        reg_info_frame.grid(row=0, column=1, padx=15, pady=5, sticky="w")

        self.lbl_stat_reg = ttk.Label(reg_info_frame, text="Status Reg (0x05): --  [ BUSY: - | WEL: - ]", font=("Consolas", 9))
        self.lbl_stat_reg.pack(anchor="w", pady=2)

        self.lbl_cfg_reg = ttk.Label(reg_info_frame, text="Config Reg (0x35): --  [ IOC: - | BPNV: - ]", font=("Consolas", 9))
        self.lbl_cfg_reg.pack(anchor="w", pady=2)

    def setup_tab_ops(self):
        ctrl_frame = ttk.LabelFrame(self.tab_ops, text=" Test Configuration ")
        ctrl_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(ctrl_frame, text="Address (Hex):").grid(row=0, column=0, padx=5, pady=5, sticky="e")
        self.ent_addr = ttk.Entry(ctrl_frame, width=10)
        self.ent_addr.insert(0, "0x000000")
        self.ent_addr.grid(row=0, column=1, padx=5, pady=5)

        ttk.Label(ctrl_frame, text="Length (Bytes):").grid(row=0, column=2, padx=5, pady=5, sticky="e")
        self.ent_len = ttk.Entry(ctrl_frame, width=10)
        self.ent_len.insert(0, "4096")
        self.ent_len.grid(row=0, column=3, padx=5, pady=5)

        ttk.Label(ctrl_frame, text="Pattern:").grid(row=0, column=4, padx=5, pady=5, sticky="e")
        self.pat_cb = ttk.Combobox(ctrl_frame, width=18, state="readonly")
        self.pat_cb['values'] = [
            "0: All 0xAA",
            "1: All 0x55",
            "2: Alt 0xAA / 0x55",
            "3: Inc (0x00-0xFF)",
            "4: All 0x00"
        ]
        self.pat_cb.current(0)
        self.pat_cb.grid(row=0, column=5, padx=5, pady=5)

        # Action Buttons
        btn_frame = ttk.Frame(self.tab_ops)
        btn_frame.pack(fill="x", padx=10, pady=5)

        ttk.Button(btn_frame, text="🗑️ 1. Erase Sector", command=self.cmd_erase).grid(row=0, column=0, padx=5, pady=5)
        ttk.Button(btn_frame, text="✍️ 2. Write Pattern", command=self.cmd_write_pat).grid(row=0, column=1, padx=5, pady=5)
        ttk.Button(btn_frame, text="🔍 3. Verify Data", command=self.cmd_verify_pat).grid(row=0, column=2, padx=5, pady=5)
        ttk.Button(btn_frame, text="📖 Read Data (Hex)", command=self.cmd_read_data).grid(row=0, column=3, padx=5, pady=5)

        # Output Log Box
        log_frame = ttk.LabelFrame(self.tab_ops, text=" Operation Log / Read Result ")
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.txt_ops_log = scrolledtext.ScrolledText(log_frame, height=10, font=("Consolas", 9), state="disabled")
        self.txt_ops_log.pack(fill="both", expand=True, padx=5, pady=5)

    def ops_log(self, text):
        self.txt_ops_log.config(state="normal")
        self.txt_ops_log.insert(tk.END, text + "\n")
        self.txt_ops_log.see(tk.END)
        self.txt_ops_log.config(state="disabled")

    def refresh_ports(self):
        ports = [port.device for port in serial.tools.list_ports.comports()]
        self.port_cb['values'] = ports
        if ports:
            self.port_cb.current(0)

    def toggle_connection(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
            self.ser = None
            self.set_led_status(False)
        else:
            port = self.port_cb.get()
            if not port:
                messagebox.showerror("Error", "No COM Port selected!")
                return
            try:
                self.ser = serial.Serial(port, 115200, timeout=3)
                self.ser.dtr = False
                self.ser.rts = False
                time.sleep(1.8)
                self.ser.reset_input_buffer()
                self.set_led_status(True)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to connect: {e}")
                self.set_led_status(False)

    def send_cmd(self, command):
        if not self.ser or not self.ser.is_open:
            messagebox.showwarning("Warning", "Please connect to COM Port first!")
            return None
        try:
            self.ser.reset_input_buffer()
            self.ser.write((command + "\n").encode('utf-8'))
            return self.ser.readline().decode('utf-8', errors='ignore').strip()
        except Exception:
            self.set_led_status(False)
            return None

    def cmd_read_id(self):
        resp = self.send_cmd("READ_ID")
        if resp and resp.startswith("ID:"):
            self.lbl_jedec.config(text=f"JEDEC ID: {resp[3:]}")
        else:
            messagebox.showerror("Error", "Failed to read JEDEC ID")

    def cmd_unlock(self):
        resp = self.send_cmd("UNLOCK")
        if resp == "UNLOCK:OK":
            messagebox.showinfo("Success", "Global Block Protection Unlocked!")
            self.cmd_read_regs()
        else:
            messagebox.showerror("Error", "Unlock failed")

    def cmd_read_regs(self):
        resp = self.send_cmd("READ_REGS")
        if resp and resp.startswith("REGS:"):
            parts = resp.split(":")
            stat_hex, cfg_hex = parts[1], parts[2]
            stat_val, cfg_val = int(stat_hex, 16), int(cfg_hex, 16)
            busy, wel = (stat_val >> 0) & 1, (stat_val >> 1) & 1
            ioc, bpnv = (cfg_val >> 1) & 1, (cfg_val >> 3) & 1
            self.lbl_stat_reg.config(text=f"Status Reg (0x05): 0x{stat_hex}  [ BUSY: {busy} | WEL: {wel} ]")
            self.lbl_cfg_reg.config(text=f"Config Reg (0x35): 0x{cfg_hex}  [ IOC: {ioc} | BPNV: {bpnv} ]")
        else:
            messagebox.showerror("Error", "Failed to read registers")

    # --- Memory Operations (Tab 2 Functions) ---
    def cmd_erase(self):
        addr = self.ent_addr.get().strip()
        self.ops_log(f"[ACTION] Erasing 4KB Sector at {addr}...")
        resp = self.send_cmd(f"ERASE:{addr}")
        if resp == "ERASE:OK":
            self.ops_log(f"[SUCCESS] Sector {addr} Erased.")
        else:
            self.ops_log(f"[FAIL] Erase failed: {resp}")

    def cmd_write_pat(self):
        addr = self.ent_addr.get().strip()
        length = self.ent_len.get().strip()
        pat_idx = self.pat_cb.get().split(":")[0]
        self.ops_log(f"[ACTION] Writing Pattern #{pat_idx} to {addr} (Len: {length} bytes)...")
        
        t0 = time.time()
        resp = self.send_cmd(f"WRITE_PAT:{addr}:{length}:{pat_idx}")
        t1 = time.time()

        if resp == "WRITE_PAT:OK":
            self.ops_log(f"[SUCCESS] Pattern Written in {t1-t0:.3f} seconds!")
        else:
            self.ops_log(f"[FAIL] Write Pattern failed: {resp}")

    def cmd_verify_pat(self):
        addr = self.ent_addr.get().strip()
        length = self.ent_len.get().strip()
        pat_idx = self.pat_cb.get().split(":")[0]
        self.ops_log(f"[ACTION] Verifying Pattern #{pat_idx} at {addr} (Len: {length} bytes)...")

        t0 = time.time()
        resp = self.send_cmd(f"VERIFY:{addr}:{length}:{pat_idx}")
        t1 = time.time()

        if resp == "VERIFY:OK":
            self.ops_log(f"[PASSED] 🟢 Verification PASSED (0 Errors) in {t1-t0:.3f}s!")
        elif resp and resp.startswith("VERIFY:FAIL:"):
            parts = resp.split(":")
            err_cnt, err_addr, detail = parts[2], parts[3], parts[4]
            self.ops_log(f"[FAILED] 🔴 Verification FAILED! ({err_cnt} errors). First Mismatch @ {err_addr} [{detail}]")
        else:
            self.ops_log(f"[FAIL] Verify command failed: {resp}")

    def cmd_read_data(self):
        addr = self.ent_addr.get().strip()
        length = self.ent_len.get().strip()
        resp = self.send_cmd(f"READ:{addr}:{length}")
        if resp and resp.startswith("DATA:"):
            self.ops_log(f"[READ @ {addr}] {resp[5:]}")
        else:
            self.ops_log(f"[FAIL] Read failed: {resp}")

if __name__ == "__main__":
    root = tk.Tk()
    app = FlashProgrammerGUI(root)
    root.mainloop()