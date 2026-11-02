import time
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import serial
import serial.tools.list_ports

class FlashProgrammerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("SST26VF016B Tester & Programmer - ESP32")
        self.root.geometry("860x600")
        self.root.resizable(False, False)
        
        self.ser = None
        self.target_hex_addr = 0x000000
        self.bytes_per_page = 256
        self.stop_requested = False

        style = ttk.Style()
        style.theme_use('clam')

        # --- Top Connection & System Bar ---
        top_bar = ttk.LabelFrame(root, text=" Connection & Configuration ")
        top_bar.pack(fill="x", padx=10, pady=5)

        ttk.Label(top_bar, text="Port:").grid(row=0, column=0, padx=(5,2), pady=8)
        self.port_cb = ttk.Combobox(top_bar, width=10, state="readonly")
        self.port_cb.grid(row=0, column=1, padx=2, pady=8)
        
        btn_refresh = ttk.Button(top_bar, text="Refresh", command=self.refresh_ports)
        btn_refresh.grid(row=0, column=2, padx=2, pady=8)

        self.btn_connect = ttk.Button(top_bar, text="Connect", command=self.toggle_connection)
        self.btn_connect.grid(row=0, column=3, padx=2, pady=8)

        self.led_canvas = tk.Canvas(top_bar, width=18, height=18, highlightthickness=0)
        self.led_canvas.grid(row=0, column=4, padx=(8, 2), pady=8)
        self.led_circle = self.led_canvas.create_oval(2, 2, 16, 16, fill="#FF3B30", outline="#B30000")

        self.lbl_status = ttk.Label(top_bar, text="Disconnected", font=("Segoe UI", 9, "bold"), foreground="#FF3B30")
        self.lbl_status.grid(row=0, column=5, padx=(2, 8), pady=8)

        ttk.Label(top_bar, text="SPI Speed:").grid(row=0, column=6, padx=(5, 2), pady=8)
        self.freq_cb = ttk.Combobox(top_bar, width=8, state="readonly")
        self.freq_cb['values'] = ["1 MHz", "5 MHz", "10 MHz", "20 MHz", "40 MHz"]
        self.freq_cb.current(2)
        self.freq_cb.grid(row=0, column=7, padx=2, pady=8)
        self.freq_cb.bind("<<ComboboxSelected>>", self.on_freq_changed)

        prog_frame = ttk.Frame(top_bar)
        prog_frame.grid(row=0, column=8, padx=(10, 5), pady=8, sticky="e")
        top_bar.grid_columnconfigure(8, weight=1)

        self.progress_var = tk.DoubleVar()
        self.progressbar = ttk.Progressbar(prog_frame, variable=self.progress_var, maximum=100, length=90)
        self.progressbar.pack(side="left", padx=2)

        self.lbl_progress = ttk.Label(prog_frame, text="0%", font=("Segoe UI", 9, "bold"), width=4)
        self.lbl_progress.pack(side="left", padx=2)

        btn_stop = tk.Button(prog_frame, text="⛔ STOP", command=self.cmd_stop, bg="#FF3B30", fg="white", font=("Segoe UI", 8, "bold"), relief="flat", padx=6)
        btn_stop.pack(side="left", padx=(5, 2))

        # --- Tab System ---
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=5)

        # Tab 1: Status & Info
        self.tab_status = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_status, text=" 📊 Device Status ")
        self.setup_tab_status()

        # Tab 2: Memory Operations (จัดระเบียบใหม่ด้วย Dropdown)
        self.tab_ops = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_ops, text=" ⚡ Memory Operations ")
        self.setup_tab_ops()

        # Tab 3: Hex Viewer (ระบบคลิกกดโหลดหน้า)
        self.tab_hex = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_hex, text=" 🔍 Hex Viewer ")
        self.setup_tab_hex()

        self.refresh_ports()

    def cmd_stop(self):
        self.stop_requested = True
        if self.ser and self.ser.is_open:
            try:
                self.ser.write(b"STOP\n")
                self.ops_log("[SYSTEM] ⛔ Stop command sent to ESP32!")
            except Exception as e:
                self.ops_log(f"[ERROR] Failed to send STOP command: {e}")

    def update_progress(self, pct):
        self.progress_var.set(pct)
        self.lbl_progress.config(text=f"{pct}%")
        self.root.update_idletasks()

    def set_led_status(self, connected):
        if connected:
            self.led_canvas.itemconfig(self.led_circle, fill="#34C759", outline="#1D8339")
            self.lbl_status.config(text="Connected", foreground="#34C759")
            self.btn_connect.config(text="Disconnect")
        else:
            self.led_canvas.itemconfig(self.led_circle, fill="#FF3B30", outline="#B30000")
            self.lbl_status.config(text="Disconnected", foreground="#FF3B30")
            self.btn_connect.config(text="Connect")
            self.update_progress(0)

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
        """จัดระเบียบหน้า Test Configuration ใหม่ให้อยู่ในระเบียบ"""
        cfg_frame = ttk.LabelFrame(self.tab_ops, text=" Execution & Pattern Parameters ")
        cfg_frame.pack(fill="x", padx=10, pady=5)

        # Row 0: Action Mode Dropdown & Scope Selection
        ttk.Label(cfg_frame, text="Operation Mode:").grid(row=0, column=0, padx=5, pady=5, sticky="e")
        self.op_mode_cb = ttk.Combobox(cfg_frame, width=22, state="readonly")
        self.op_mode_cb['values'] = [
            "✍️ Write Pattern",
            "🔍 Verify Pattern",
            "🗑️ Erase Sector (4KB)",
            "💥 Erase Full Chip (2MB)",
            "📖 Read Data (Hex)"
        ]
        self.op_mode_cb.current(0)
        self.op_mode_cb.grid(row=0, column=1, padx=5, pady=5)
        self.op_mode_cb.bind("<<ComboboxSelected>>", self.on_op_mode_changed)

        ttk.Label(cfg_frame, text="Scope:").grid(row=0, column=2, padx=5, pady=5, sticky="e")
        self.scope_cb = ttk.Combobox(cfg_frame, width=16, state="readonly")
        self.scope_cb['values'] = ["Selected Range", "Entire Chip (2MB)"]
        self.scope_cb.current(0)
        self.scope_cb.grid(row=0, column=3, padx=5, pady=5)

        # Row 1: Address & Length
        ttk.Label(cfg_frame, text="Address (Hex):").grid(row=1, column=0, padx=5, pady=5, sticky="e")
        self.ent_addr = ttk.Entry(cfg_frame, width=12)
        self.ent_addr.insert(0, "0x000000")
        self.ent_addr.grid(row=1, column=1, padx=5, pady=5)

        ttk.Label(cfg_frame, text="Length (Bytes):").grid(row=1, column=2, padx=5, pady=5, sticky="e")
        self.ent_len = ttk.Entry(cfg_frame, width=12)
        self.ent_len.insert(0, "4096")
        self.ent_len.grid(row=1, column=3, padx=5, pady=5)

        # Row 2: Pattern Select & Custom Byte Input
        ttk.Label(cfg_frame, text="Pattern Type:").grid(row=2, column=0, padx=5, pady=5, sticky="e")
        self.pat_cb = ttk.Combobox(cfg_frame, width=22, state="readonly")
        self.pat_cb['values'] = [
            "0: All 0xAA",
            "1: All 0x55",
            "2: Alt 0xAA / 0x55",
            "3: Inc (0x00-0xFF)",
            "4: All 0x00",
            "5: 🎲 Pseudo-Random (สุ่ม)",
            "6: ✏️ Custom Hex (กำหนดเอง)"
        ]
        self.pat_cb.current(0)
        self.pat_cb.grid(row=2, column=1, padx=5, pady=5)
        self.pat_cb.bind("<<ComboboxSelected>>", self.on_pattern_changed)

        ttk.Label(cfg_frame, text="Custom Hex (0x):").grid(row=2, column=2, padx=5, pady=5, sticky="e")
        self.ent_custom_hex = ttk.Entry(cfg_frame, width=12, state="disabled")
        self.ent_custom_hex.insert(0, "AA")
        self.ent_custom_hex.grid(row=2, column=3, padx=5, pady=5)

        # Action Execution Bar
        act_frame = ttk.Frame(self.tab_ops)
        act_frame.pack(fill="x", padx=10, pady=5)

        btn_exec = ttk.Button(act_frame, text="▶ ดำเนินการ (Execute Operation)", command=self.cmd_execute_action)
        btn_exec.pack(side="left", padx=5)

        btn_clear = ttk.Button(act_frame, text="🧹 Clear Log", command=self.cmd_clear_log)
        btn_clear.pack(side="right", padx=5)

        # Output Log Box
        log_frame = ttk.LabelFrame(self.tab_ops, text=" Operation Log ")
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.txt_ops_log = scrolledtext.ScrolledText(log_frame, height=10, font=("Consolas", 9), state="disabled")
        self.txt_ops_log.pack(fill="both", expand=True, padx=5, pady=5)

    def on_op_mode_changed(self, event=None):
        mode = self.op_mode_cb.get()
        if "Full Chip" in mode or "Sector" in mode:
            if "Full Chip" in mode:
                self.scope_cb.set("Entire Chip (2MB)")
            self.ent_len.config(state="disabled")
        else:
            self.ent_len.config(state="normal")

    def on_pattern_changed(self, event=None):
        pat_idx = self.pat_cb.get().split(":")[0]
        if pat_idx == "6":
            self.ent_custom_hex.config(state="normal")
        else:
            self.ent_custom_hex.config(state="disabled")

    def setup_tab_hex(self):
        """ระบบ Hex Viewer ที่เปลี่ยนหน้าเฉพาะตัวเลข และต้องกดปุ่มเพื่อโหลดข้อมูล"""
        nav_frame = ttk.LabelFrame(self.tab_hex, text=" Navigation & Page Selection ")
        nav_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(nav_frame, text="Target Address (Hex):").grid(row=0, column=0, padx=5, pady=5)
        self.ent_hex_jump = ttk.Entry(nav_frame, width=12)
        self.ent_hex_jump.insert(0, "0x000000")
        self.ent_hex_jump.grid(row=0, column=1, padx=5, pady=5)

        ttk.Button(nav_frame, text="◀ Prev Page", command=self.cmd_hex_prev_page).grid(row=0, column=2, padx=5, pady=5)
        ttk.Button(nav_frame, text="Next Page ▶", command=self.cmd_hex_next_page).grid(row=0, column=3, padx=5, pady=5)
        
        # ปุ่มกดโหลดหน้า (โหลดเมื่อผู้ใช้กดเท่านั้น)
        btn_load_hex = ttk.Button(nav_frame, text="📥 โหลดข้อมูล (Load Page)", command=self.load_hex_view_page)
        btn_load_hex.grid(row=0, column=4, padx=10, pady=5)

        self.lbl_hex_page_info = ttk.Label(nav_frame, text="Target Page: 0x000000 - 0x0000FF", font=("Consolas", 9, "bold"))
        self.lbl_hex_page_info.grid(row=0, column=5, padx=15, pady=5)

        view_frame = ttk.LabelFrame(self.tab_hex, text=" Data Memory Array ")
        view_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.txt_hex_display = scrolledtext.ScrolledText(view_frame, font=("Consolas", 10), state="disabled", bg="#1E1E1E", fg="#D4D4D4")
        self.txt_hex_display.pack(fill="both", expand=True, padx=5, pady=5)

    def update_hex_page_label(self):
        self.lbl_hex_page_info.config(text=f"Target Page: 0x{self.target_hex_addr:06X} - 0x{self.target_hex_addr + self.bytes_per_page - 1:06X}")

    def cmd_hex_prev_page(self):
        if self.target_hex_addr >= self.bytes_per_page:
            self.target_hex_addr -= self.bytes_per_page
            self.ent_hex_jump.delete(0, tk.END)
            self.ent_hex_jump.insert(0, f"0x{self.target_hex_addr:06X}")
            self.update_hex_page_label()

    def cmd_hex_next_page(self):
        if self.target_hex_addr + self.bytes_per_page < 0x200000:
            self.target_hex_addr += self.bytes_per_page
            self.ent_hex_jump.delete(0, tk.END)
            self.ent_hex_jump.insert(0, f"0x{self.target_hex_addr:06X}")
            self.update_hex_page_label()

    def load_hex_view_page(self):
        """โหลดข้อมูลอ่านเฉพาะตอนกดปุ่มเท่านั้น"""
        try:
            addr = int(self.ent_hex_jump.get().strip(), 16)
            addr = addr & ~0xF
            if 0 <= addr < 0x200000:
                self.target_hex_addr = addr
                self.update_hex_page_label()
            else:
                messagebox.showerror("Error", "Address out of range (0x000000 - 0x1FFFFF)")
                return
        except ValueError:
            messagebox.showerror("Error", "Invalid Hex Address format")
            return

        addr_str = f"0x{self.target_hex_addr:06X}"
        resp = self.execute_cmd_with_progress(f"READ:{addr_str}:{self.bytes_per_page}", ["DATA:"])
        if not resp or not resp.startswith("DATA:"):
            return

        raw_hex_list = resp[5:].strip().split(" ")
        try:
            raw_bytes = bytes([int(x, 16) for x in raw_hex_list])
        except ValueError:
            return

        lines = []
        header = "Offset    00 01 02 03 04 05 06 07  08 09 0A 0B 0C 0D 0E 0F  Decoded ASCII\n"
        header += "-" * 77 + "\n"
        lines.append(header)

        for i in range(0, len(raw_bytes), 16):
            chunk = raw_bytes[i:i+16]
            row_addr = f"{self.target_hex_addr + i:08X}"
            
            hex_b1 = " ".join(f"{b:02X}" for b in chunk[:8])
            hex_b2 = " ".join(f"{b:02X}" for b in chunk[8:])
            
            hex_part = f"{hex_b1:<23}  {hex_b2:<23}"
            ascii_part = "".join(chr(b) if 32 <= b <= 126 else "." for b in chunk)
            
            lines.append(f"{row_addr}  {hex_part}  |{ascii_part}|")

        self.txt_hex_display.config(state="normal")
        self.txt_hex_display.delete('1.0', tk.END)
        self.txt_hex_display.insert(tk.END, "\n".join(lines))
        self.txt_hex_display.config(state="disabled")

    def ops_log(self, text):
        self.txt_ops_log.config(state="normal")
        self.txt_ops_log.insert(tk.END, text + "\n")
        self.txt_ops_log.see(tk.END)
        self.txt_ops_log.config(state="disabled")

    def cmd_clear_log(self):
        self.txt_ops_log.config(state="normal")
        self.txt_ops_log.delete('1.0', tk.END)
        self.txt_ops_log.config(state="disabled")

    def refresh_ports(self):
        ports = [port.device for port in serial.tools.list_ports.comports()]
        self.port_cb['values'] = ports
        if ports:
            self.port_cb.current(0)

    def on_freq_changed(self, event=None):
        if self.ser and self.ser.is_open:
            freq_str = self.freq_cb.get().split(" ")[0]
            resp = self.send_raw_cmd(f"SET_FREQ:{freq_str}")
            if resp and resp.startswith("FREQ:OK"):
                self.ops_log(f"[SYSTEM] SPI Frequency changed to {freq_str} MHz")

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
                self.ser = serial.Serial(port, 921600, timeout=3)
                self.ser.dtr = False
                self.ser.rts = False
                time.sleep(1.8)
                self.ser.reset_input_buffer()
                self.set_led_status(True)
                self.on_freq_changed()
            except Exception as e:
                messagebox.showerror("Error", f"Failed to connect: {e}")
                self.set_led_status(False)

    def send_raw_cmd(self, command):
        try:
            self.ser.reset_input_buffer()
            self.ser.write((command + "\n").encode('utf-8'))
            return self.ser.readline().decode('utf-8', errors='ignore').strip()
        except Exception:
            return None

    def execute_cmd_with_progress(self, command, expected_end_tokens, timeout=60.0):
        if not self.ser or not self.ser.is_open:
            messagebox.showwarning("Warning", "Please connect to COM Port first!")
            return None

        self.stop_requested = False
        self.update_progress(0)
        
        try:
            self.ser.reset_input_buffer()
            self.ser.write((command + "\n").encode('utf-8'))

            old_timeout = self.ser.timeout
            self.ser.timeout = timeout
            start_time = time.time()
            final_resp = None
            printed_errors = 0

            while True:
                if self.stop_requested:
                    self.ser.write(b"STOP\n")
                    self.ops_log("[ABORTED] Operation stopped by user!")
                    self.update_progress(0)
                    break

                if time.time() - start_time > timeout:
                    self.ops_log("[ERROR] Operation timed out!")
                    break

                line = self.ser.readline().decode('utf-8', errors='ignore').strip()
                if not line:
                    continue

                if line.startswith("PROGRESS:"):
                    try:
                        pct = int(line.split(":")[1])
                        self.update_progress(pct)
                    except ValueError:
                        pass
                elif line.startswith("FAIL_ADDR:"):
                    printed_errors += 1
                    if printed_errors <= 20:
                        parts = line.split(":")
                        self.ops_log(f"  ❌ Mismatch @ {parts[1]} | Details: {parts[2]}")
                    elif printed_errors == 21:
                        self.ops_log("  ⚠️ [NOTICE] Mismatch count exceeds 20. Suppressing further error prints...")
                    self.root.update()
                elif line == "ABORTED":
                    self.ops_log("[ABORTED] Hardware operation canceled.")
                    self.update_progress(0)
                    break
                else:
                    for token in expected_end_tokens:
                        if line.startswith(token) or line == token:
                            final_resp = line
                            break
                    if final_resp:
                        break

            self.ser.timeout = old_timeout
            return final_resp
        except Exception as e:
            self.ops_log(f"[ERROR] Communication failure: {e}")
            self.set_led_status(False)
            return None

    def get_custom_hex_val(self):
        val_str = self.ent_custom_hex.get().strip()
        try:
            val = int(val_str, 16)
            return f"{val:02X}"
        except ValueError:
            return "00"

    def cmd_execute_action(self):
        """ประมวลผลตาม Operation Mode และ Scope ที่เลือก"""
        mode = self.op_mode_cb.get()
        scope = self.scope_cb.get()
        addr = self.ent_addr.get().strip()
        length = self.ent_len.get().strip()
        pat_idx = self.pat_cb.get().split(":")[0]
        custom_hex = self.get_custom_hex_val()

        if "Write Pattern" in mode:
            if scope == "Entire Chip (2MB)":
                self.ops_log(f"[ACTION] Writing Pattern #{pat_idx} (Custom: 0x{custom_hex}) to ENTIRE CHIP (2MB)...")
                t0 = time.time()
                resp = self.execute_cmd_with_progress(f"WRITE_FULL_PAT:{pat_idx}:{custom_hex}", ["WRITE_PAT:OK"])
                t1 = time.time()
                if resp == "WRITE_PAT:OK":
                    self.ops_log(f"[SUCCESS] 🚀 Full Chip Written in {t1-t0:.2f}s!")
            else:
                self.ops_log(f"[ACTION] Writing Pattern #{pat_idx} to {addr} (Len: {length} bytes)...")
                t0 = time.time()
                resp = self.execute_cmd_with_progress(f"WRITE_PAT:{addr}:{length}:{pat_idx}:{custom_hex}", ["WRITE_PAT:OK"])
                t1 = time.time()
                if resp == "WRITE_PAT:OK":
                    self.ops_log(f"[SUCCESS] Pattern Written in {t1-t0:.3f}s!")

        elif "Verify Pattern" in mode:
            if scope == "Entire Chip (2MB)":
                self.ops_log(f"[ACTION] Verifying ENTIRE CHIP with Pattern #{pat_idx}...")
                t0 = time.time()
                resp = self.execute_cmd_with_progress(f"VERIFY_FULL_PAT:{pat_idx}:{custom_hex}", ["VERIFY_FULL:DONE:"])
                t1 = time.time()
                if resp and resp.startswith("VERIFY_FULL:DONE:"):
                    total_errors = int(resp.split(":")[2])
                    if total_errors == 0:
                        self.ops_log(f"[PASSED] 🟢 Full Chip Verification PASSED in {t1-t0:.2f}s!")
                    else:
                        self.ops_log(f"[FAILED] 🔴 Full Chip Verification FAILED! Total {total_errors:,} Mismatched Bytes.")
            else:
                self.ops_log(f"[ACTION] Verifying Pattern #{pat_idx} at {addr} (Len: {length} bytes)...")
                t0 = time.time()
                resp = self.execute_cmd_with_progress(f"VERIFY:{addr}:{length}:{pat_idx}:{custom_hex}", ["VERIFY:OK", "VERIFY:FAIL:"])
                t1 = time.time()
                if resp == "VERIFY:OK":
                    self.ops_log(f"[PASSED] 🟢 Verification PASSED (0 Errors) in {t1-t0:.3f}s!")
                elif resp and resp.startswith("VERIFY:FAIL:"):
                    parts = resp.split(":")
                    self.ops_log(f"[FAILED] 🔴 Verification FAILED! ({parts[2]} errors). First Mismatch @ {parts[3]} [{parts[4]}]")

        elif "Erase Sector" in mode:
            self.ops_log(f"[ACTION] Erasing Sector at {addr}...")
            resp = self.execute_cmd_with_progress(f"ERASE:{addr}", ["ERASE:OK"])
            if resp == "ERASE:OK":
                self.ops_log(f"[SUCCESS] Sector {addr} Erased.")

        elif "Erase Full Chip" in mode:
            if messagebox.askyesno("Confirm Erase", "Are you sure you want to erase ENTIRE CHIP (2MB)?"):
                self.ops_log("[ACTION] Erasing ENTIRE CHIP (2 Megabytes)... Please wait...")
                resp = self.execute_cmd_with_progress("ERASE_CHIP", ["ERASE_CHIP:OK"])
                if resp == "ERASE_CHIP:OK":
                    self.ops_log("[SUCCESS] 💥 Entire Chip Erased Successfully!")

        elif "Read Data" in mode:
            resp = self.execute_cmd_with_progress(f"READ:{addr}:{length}", ["DATA:"])
            if resp and resp.startswith("DATA:"):
                self.ops_log(f"[READ @ {addr}] {resp[5:]}")

    def cmd_read_id(self):
        resp = self.execute_cmd_with_progress("READ_ID", ["ID:"])
        if resp and resp.startswith("ID:"):
            self.lbl_jedec.config(text=f"JEDEC ID: {resp[3:]}")
        else:
            messagebox.showerror("Error", "Failed to read JEDEC ID")

    def cmd_unlock(self):
        resp = self.execute_cmd_with_progress("UNLOCK", ["UNLOCK:OK"])
        if resp == "UNLOCK:OK":
            messagebox.showinfo("Success", "Global Block Protection Unlocked!")
            self.cmd_read_regs()
        else:
            messagebox.showerror("Error", "Unlock failed")

    def cmd_read_regs(self):
        resp = self.execute_cmd_with_progress("READ_REGS", ["REGS:"])
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

if __name__ == "__main__":
    root = tk.Tk()
    app = FlashProgrammerGUI(root)
    root.mainloop()