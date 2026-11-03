import time
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import serial
import serial.tools.list_ports

class FlashProgrammerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("SST26VF016B Tester & Programmer - ESP32")
        self.root.geometry("1000x700")
        self.root.resizable(False, False)
        
        self.ser = None
        self.target_hex_addr = 0x000000
        self.bytes_per_page = 256
        self.stop_requested = False

        self.pin_colors = {i: "#777777" for i in range(1, 9)}

        style = ttk.Style()
        style.theme_use('clam')

        # =========================================================================
        # TOP HEADER: Serial Connection & Status Control
        # =========================================================================
        top_bar = ttk.LabelFrame(root, text=" System Connection & Control Status ")
        top_bar.pack(fill="x", padx=10, pady=5)

        row0 = ttk.Frame(top_bar)
        row0.pack(fill="x", padx=5, pady=4)

        ttk.Label(row0, text="Port:").pack(side="left", padx=2)
        self.port_cb = ttk.Combobox(row0, width=10, state="readonly")
        self.port_cb.pack(side="left", padx=2)
        
        btn_refresh = ttk.Button(row0, text="Refresh", command=self.refresh_ports)
        btn_refresh.pack(side="left", padx=2)

        self.btn_connect = ttk.Button(row0, text="Connect", command=self.toggle_connection)
        self.btn_connect.pack(side="left", padx=2)

        self.led_canvas = tk.Canvas(row0, width=16, height=16, highlightthickness=0)
        self.led_canvas.pack(side="left", padx=(8, 2))
        self.led_circle = self.led_canvas.create_oval(2, 2, 14, 14, fill="#FF3B30", outline="#B30000")

        self.lbl_status = ttk.Label(row0, text="Disconnected", font=("Segoe UI", 9, "bold"), foreground="#FF3B30")
        self.lbl_status.pack(side="left", padx=2)

        ttk.Label(row0, text="SPI Speed:").pack(side="left", padx=(10, 2))
        self.freq_cb = ttk.Combobox(row0, width=8, state="readonly")
        self.freq_cb['values'] = ["1 MHz", "5 MHz", "10 MHz", "20 MHz", "40 MHz"]
        self.freq_cb.current(2)
        self.freq_cb.pack(side="left", padx=2)
        self.freq_cb.bind("<<ComboboxSelected>>", self.on_freq_changed)

        prog_frame = ttk.Frame(row0)
        prog_frame.pack(side="right", padx=5)

        self.progress_var = tk.DoubleVar()
        self.progressbar = ttk.Progressbar(prog_frame, variable=self.progress_var, maximum=100, length=100)
        self.progressbar.pack(side="left", padx=2)

        self.lbl_progress = ttk.Label(prog_frame, text="0%", font=("Segoe UI", 9, "bold"), width=4)
        self.lbl_progress.pack(side="left", padx=2)

        btn_stop = tk.Button(prog_frame, text="⛔ STOP", command=self.cmd_stop, bg="#FF3B30", fg="white", font=("Segoe UI", 8, "bold"), relief="flat", padx=6)
        btn_stop.pack(side="left", padx=(5, 2))

        row1 = ttk.Frame(top_bar)
        row1.pack(fill="x", padx=5, pady=(2, 4))

        ttk.Button(row1, text="Read JEDEC ID", command=self.cmd_read_id).pack(side="left", padx=2)
        ttk.Button(row1, text="Unlock Protection", command=self.cmd_unlock).pack(side="left", padx=2)
        ttk.Button(row1, text="Read Registers", command=self.cmd_read_regs).pack(side="left", padx=2)

        self.lbl_jedec = ttk.Label(row1, text="JEDEC ID: -- -- --", font=("Consolas", 9, "bold"))
        self.lbl_jedec.pack(side="left", padx=(12, 6))

        self.lbl_stat_reg = ttk.Label(row1, text="SR(0x05): --", font=("Consolas", 9))
        self.lbl_stat_reg.pack(side="left", padx=6)

        self.lbl_cfg_reg = ttk.Label(row1, text="CR(0x35): --", font=("Consolas", 9))
        self.lbl_cfg_reg.pack(side="left", padx=6)

        self.lbl_phase = ttk.Label(row1, text="[ State: IDLE ]", font=("Consolas", 9, "bold"), foreground="#007ACC")
        self.lbl_phase.pack(side="right", padx=5)

        # =========================================================================
        # TAB SYSTEM
        # =========================================================================
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=5)

        self.tab_ops = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_ops, text=" ⚡ Memory Operations ")
        self.setup_tab_ops()

        self.tab_hex = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_hex, text=" 🔍 Hex Viewer ")
        self.setup_tab_hex()

        self.refresh_ports()

    def draw_ic_diagram(self):
        self.ic_canvas.delete("all")
        self.ic_canvas.create_rectangle(70, 15, 170, 95, fill="#2B2B2B", outline="#1E1E1E")
        self.ic_canvas.create_text(120, 55, text="SST26VF016B\n(SOP8)", fill="#FFFFFF", font=("Segoe UI", 7, "bold"), justify="center")
        self.ic_canvas.create_oval(78, 22, 84, 28, fill="#888888", outline="")

        pins_def = [
            (1, "CE#", "LEFT", 28),
            (2, "MISO", "LEFT", 46),
            (3, "WP#", "LEFT", 64),
            (4, "GND", "LEFT", 82),
            (8, "VDD", "RIGHT", 28),
            (7, "HOLD#", "RIGHT", 46),
            (6, "SCK", "RIGHT", 64),
            (5, "MOSI", "RIGHT", 82)
        ]

        for p_num, name, side, y in pins_def:
            color = self.pin_colors[p_num]
            if side == "LEFT":
                self.ic_canvas.create_line(50, y, 70, y, fill="#777777", width=3)
                self.ic_canvas.create_oval(38, y-5, 48, y+5, fill=color, outline="#333333")
                self.ic_canvas.create_text(20, y, text=f"{p_num}:{name}", font=("Consolas", 7, "bold"), anchor="e")
            else:
                self.ic_canvas.create_line(170, y, 190, y, fill="#777777", width=3)
                self.ic_canvas.create_oval(192, y-5, 202, y+5, fill=color, outline="#333333")
                self.ic_canvas.create_text(206, y, text=f"{p_num}:{name}", font=("Consolas", 7, "bold"), anchor="w")

    def update_pin_health(self, pin_dict):
        for p_num, color in pin_dict.items():
            if p_num in self.pin_colors:
                self.pin_colors[p_num] = color
        self.draw_ic_diagram()

    def reset_all_pins(self):
        self.pin_colors = {i: "#777777" for i in range(1, 9)}
        self.draw_ic_diagram()

    def update_phase_status(self, text):
        self.lbl_phase.config(text=text)
        self.root.update_idletasks()

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
            self.update_phase_status("[ State: IDLE ]")
            self.reset_all_pins()

    def auto_jump_to_hex_view(self, fail_addr_hex):
        try:
            addr = int(fail_addr_hex, 16) if isinstance(fail_addr_hex, str) else fail_addr_hex
            addr = addr & ~0xF
            self.target_hex_addr = addr
            self.ent_hex_jump.delete(0, tk.END)
            self.ent_hex_jump.insert(0, f"0x{self.target_hex_addr:06X}")
            self.update_hex_page_label()
            
            self.notebook.select(1)
            self.ops_log(f"[AUTO-JUMP] 🔍 Switched to Hex Viewer for failing page @ 0x{self.target_hex_addr:06X}")
            self.load_hex_view_page()
        except Exception as e:
            self.ops_log(f"[ERROR] Auto-Jump failed: {e}")

    # -------------------------------------------------------------------------
    # Auto-Sync Handlers for Page No. & Hex Address
    # -------------------------------------------------------------------------
    def on_page_changed(self, event=None):
        val = self.ent_page.get().strip()
        if val.isdigit():
            page_num = int(val)
            if 0 <= page_num <= 8191:
                addr = page_num * 256
                self.ent_addr.delete(0, tk.END)
                self.ent_addr.insert(0, f"0x{addr:06X}")

    def on_addr_changed(self, event=None):
        val = self.ent_addr.get().strip()
        try:
            addr = int(val, 16)
            if 0 <= addr < 0x200000:
                page_num = addr // 256
                self.ent_page.delete(0, tk.END)
                self.ent_page.insert(0, str(page_num))
        except ValueError:
            pass

    def setup_tab_ops(self):
        top_ops_container = ttk.Frame(self.tab_ops)
        top_ops_container.pack(fill="x", padx=10, pady=5)

        left_ops_frame = ttk.Frame(top_ops_container)
        left_ops_frame.pack(side="left", fill="both", expand=True)

        cfg_frame = ttk.LabelFrame(left_ops_frame, text=" Execution & Pattern Parameters ")
        cfg_frame.pack(fill="x", padx=2, pady=2)

        # Row 0: Operation Mode & Scope
        ttk.Label(cfg_frame, text="Operation Mode:").grid(row=0, column=0, padx=5, pady=4, sticky="e")
        self.op_mode_cb = ttk.Combobox(cfg_frame, width=22, state="readonly")
        self.op_mode_cb['values'] = [
            "✍️ Write Pattern",
            "🔍 Verify Pattern",
            "🗑️ Erase Sector (4KB)",
            "📖 Read Data (Hex)",
            "🚀 All Patterns Sweep (Full Chip)"
        ]
        self.op_mode_cb.current(0)
        self.op_mode_cb.grid(row=0, column=1, padx=5, pady=4)
        self.op_mode_cb.bind("<<ComboboxSelected>>", self.on_op_mode_changed)

        ttk.Label(cfg_frame, text="Scope / Loop:").grid(row=0, column=2, padx=5, pady=4, sticky="e")
        self.scope_cb = ttk.Combobox(cfg_frame, width=16, state="readonly")
        self.scope_cb['values'] = ["Selected Range", "Entire Chip (2MB)", "Continuous Loop"]
        self.scope_cb.current(0)
        self.scope_cb.grid(row=0, column=3, padx=5, pady=4)

        # Row 1: Page Selection & Hex Address (With Total Pages Label)
        ttk.Label(cfg_frame, text="Page No.:").grid(row=1, column=0, padx=5, pady=4, sticky="e")
        
        page_input_frame = ttk.Frame(cfg_frame)
        page_input_frame.grid(row=1, column=1, padx=5, pady=4, sticky="w")
        
        self.ent_page = ttk.Entry(page_input_frame, width=8)
        self.ent_page.insert(0, "0")
        self.ent_page.pack(side="left", padx=(0, 4))
        self.ent_page.bind("<KeyRelease>", self.on_page_changed)

        ttk.Label(page_input_frame, text="(0-8191 | Total 8,192 Pages)", font=("Segoe UI", 8, "italic"), foreground="#555555").pack(side="left")

        ttk.Label(cfg_frame, text="Address (Hex):").grid(row=1, column=2, padx=5, pady=4, sticky="e")
        self.ent_addr = ttk.Entry(cfg_frame, width=12)
        self.ent_addr.insert(0, "0x000000")
        self.ent_addr.grid(row=1, column=3, padx=5, pady=4)
        self.ent_addr.bind("<KeyRelease>", self.on_addr_changed)

        # Row 2: Length & Pattern Selection
        ttk.Label(cfg_frame, text="Length (Bytes):").grid(row=2, column=0, padx=5, pady=4, sticky="e")
        self.ent_len = ttk.Entry(cfg_frame, width=12)
        self.ent_len.insert(0, "4096")
        self.ent_len.grid(row=2, column=1, padx=5, pady=4, sticky="w")

        ttk.Label(cfg_frame, text="Pattern Type:").grid(row=2, column=2, padx=5, pady=4, sticky="e")
        self.pat_cb = ttk.Combobox(cfg_frame, width=22, state="readonly")
        self.pat_cb['values'] = [
            "0: All 0xAA",
            "1: All 0x55",
            "2: Alt 0xAA / 0x55",
            "3: Inc (0x00-0xFF)",
            "4: All 0x00",
            "5: 🎲 Pseudo-Random",
            "6: ✏️ Custom Hex"
        ]
        self.pat_cb.current(0)
        self.pat_cb.grid(row=2, column=3, padx=5, pady=4)
        self.pat_cb.bind("<<ComboboxSelected>>", self.on_pattern_changed)

        # Row 3: Custom Hex Input
        ttk.Label(cfg_frame, text="Custom Hex (0x):").grid(row=3, column=2, padx=5, pady=4, sticky="e")
        self.ent_custom_hex = ttk.Entry(cfg_frame, width=12, state="disabled")
        self.ent_custom_hex.insert(0, "AA")
        self.ent_custom_hex.grid(row=3, column=3, padx=5, pady=4)

        act_frame = ttk.Frame(left_ops_frame)
        act_frame.pack(fill="x", padx=2, pady=4)

        btn_exec = ttk.Button(act_frame, text="▶ Execute Operation", command=self.cmd_execute_action)
        btn_exec.pack(side="left", padx=3)

        btn_all_pat = ttk.Button(act_frame, text="🔥 Run All Patterns (1-Click Sweep)", command=self.run_all_pattern_sweep_engine)
        btn_all_pat.pack(side="left", padx=3)

        btn_chip_erase = ttk.Button(act_frame, text="💥 Erase Full Chip", command=self.cmd_standalone_erase_chip)
        btn_chip_erase.pack(side="left", padx=3)

        btn_clear = ttk.Button(act_frame, text="🧹 Clear Log", command=self.cmd_clear_log)
        btn_clear.pack(side="right", padx=3)

        ic_frame = ttk.LabelFrame(top_ops_container, text=" IC Pin Health Diagram ")
        ic_frame.pack(side="right", fill="y", padx=(8, 2), pady=2)

        self.ic_canvas = tk.Canvas(ic_frame, width=240, height=125, bg="#F0F0F0", highlightthickness=0)
        self.ic_canvas.pack(padx=8, pady=4)
        self.draw_ic_diagram()

        log_frame = ttk.LabelFrame(self.tab_ops, text=" Operation Log ")
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.txt_ops_log = scrolledtext.ScrolledText(log_frame, font=("Consolas", 9), state="disabled")
        self.txt_ops_log.pack(fill="both", expand=True, padx=5, pady=5)

    def on_op_mode_changed(self, event=None):
        mode = self.op_mode_cb.get()
        if "Sweep" in mode:
            self.scope_cb.set("Entire Chip (2MB)")
            self.ent_len.config(state="disabled")
            self.pat_cb.config(state="disabled")
        else:
            self.ent_len.config(state="normal")
            self.pat_cb.config(state="readonly")

    def on_pattern_changed(self, event=None):
        pat_idx = self.pat_cb.get().split(":")[0]
        if pat_idx == "6":
            self.ent_custom_hex.config(state="normal")
        else:
            self.ent_custom_hex.config(state="disabled")

    def setup_tab_hex(self):
        nav_frame = ttk.LabelFrame(self.tab_hex, text=" Navigation & Page Selection ")
        nav_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(nav_frame, text="Target Address (Hex):").grid(row=0, column=0, padx=5, pady=5)
        self.ent_hex_jump = ttk.Entry(nav_frame, width=12)
        self.ent_hex_jump.insert(0, "0x000000")
        self.ent_hex_jump.grid(row=0, column=1, padx=5, pady=5)

        ttk.Button(nav_frame, text="◀ Prev Page", command=self.cmd_hex_prev_page).grid(row=0, column=2, padx=5, pady=5)
        ttk.Button(nav_frame, text="Next Page ▶", command=self.cmd_hex_next_page).grid(row=0, column=3, padx=5, pady=5)
        
        btn_load_hex = ttk.Button(nav_frame, text="📥 Load Page", command=self.load_hex_view_page)
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
            self.ser.timeout = 0.1
            start_time = time.time()
            final_resp = None
            printed_errors = 0

            while True:
                self.root.update()
                if self.stop_requested:
                    self.ser.write(b"STOP\n")
                    self.ops_log("[ABORTED] Operation stopped by user!")
                    self.update_progress(0)
                    self.update_phase_status("[ State: IDLE ]")
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
                elif line == "ABORTED":
                    self.ops_log("[ABORTED] Hardware operation canceled.")
                    self.update_progress(0)
                    self.update_phase_status("[ State: IDLE ]")
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

    def run_all_pattern_sweep_engine(self):
        if not self.ser or not self.ser.is_open:
            messagebox.showwarning("Warning", "Please connect to COM Port first!")
            return

        is_loop = 1 if self.scope_cb.get() == "Continuous Loop" else 0
        self.stop_requested = False
        
        self.ops_log(f"\n==========================================")
        self.ops_log(f"🚀 STARTING ALL-PATTERN FULL-CHIP SWEEP")
        self.ops_log(f"==========================================")
        
        self.ser.reset_input_buffer()
        self.ser.write(f"ALL_PAT_START:{is_loop}\n".encode('utf-8'))

        pat_names = ["0xAA", "0x55", "Alt 0xAA/0x55", "Inc (0x00-0xFF)", "All 0x00", "Pseudo-Random"]
        curr_cycle = 1
        curr_pat = 0
        curr_phase = "START"
        printed_errors = 0

        old_timeout = self.ser.timeout
        self.ser.timeout = 0.1

        first_failed_addr = None

        while not self.stop_requested:
            self.root.update()
            line = self.ser.readline().decode('utf-8', errors='ignore').strip()
            if not line:
                continue

            if line.startswith("SWEEP_CYCLE_START:"):
                curr_cycle = int(line.split(":")[1])
                self.ops_log(f"\n🔄 --- Sweep Cycle #{curr_cycle} ---")

            elif line.startswith("PAT_START:"):
                curr_pat = int(line.split(":")[1])
                printed_errors = 0
                p_name = pat_names[curr_pat] if curr_pat < len(pat_names) else f"#{curr_pat}"
                self.ops_log(f"  ▶ Pattern #{curr_pat} ({p_name})...")
                self.update_phase_status(f"[ Cycle #{curr_cycle} | Pat: #{curr_pat} ]")

            elif line.startswith("PHASE:"):
                curr_phase = line.split(":")[1]
                p_name = pat_names[curr_pat] if curr_pat < len(pat_names) else f"#{curr_pat}"
                self.update_phase_status(f"[ Pat: #{curr_pat} ({p_name}) | Phase: {curr_phase} ]")

            elif line.startswith("PROGRESS:"):
                try:
                    pct = int(line.split(":")[1])
                    self.update_progress(pct)
                except ValueError:
                    pass

            elif line.startswith("FAIL_ADDR:"):
                printed_errors += 1
                parts = line.split(":")
                if first_failed_addr is None:
                    first_failed_addr = parts[1]
                if printed_errors <= 20:
                    self.ops_log(f"    ❌ Mismatch @ {parts[1]} | {parts[2]}")

            elif line.startswith("PAT_DONE:"):
                parts = line.split(":")
                pat_id, errs = int(parts[1]), int(parts[2])
                p_name = pat_names[pat_id] if pat_id < len(pat_names) else f"#{pat_id}"
                if errs == 0:
                    self.ops_log(f"  🟢 Pat #{pat_id} ({p_name}) PASSED!")
                    self.update_pin_health({2: "#34C759", 5: "#34C759"})
                else:
                    self.ops_log(f"  🔴 Pat #{pat_id} ({p_name}) FAILED! Total {errs:,} Errors")
                    self.update_pin_health({2: "#FF3B30", 5: "#FF3B30"})

            elif line.startswith("SWEEP_CYCLE_DONE:"):
                c_num = line.split(":")[1]
                self.ops_log(f"✅ Sweep Cycle #{c_num} Completed!")
                if not is_loop:
                    break

            elif line == "ABORTED":
                self.ops_log("[ABORTED] Sweep operation canceled by user.")
                break

        self.ser.timeout = old_timeout
        self.update_progress(0)
        self.update_phase_status("[ State: IDLE ]")

        if first_failed_addr:
            self.auto_jump_to_hex_view(first_failed_addr)

    def cmd_standalone_erase_chip(self):
        if messagebox.askyesno("Confirm Chip Erase", "Are you sure you want to erase the ENTIRE CHIP (2MB)?"):
            self.update_phase_status("[ Phase: ERASING CHIP... ]")
            self.ops_log("[ACTION] Erasing ENTIRE CHIP (2 Megabytes)... Please wait...")
            resp = self.execute_cmd_with_progress("ERASE_CHIP", ["ERASE_CHIP:OK"])
            if resp == "ERASE_CHIP:OK":
                self.ops_log("[SUCCESS] 💥 Entire Chip Erased Successfully!")
            self.update_phase_status("[ State: IDLE ]")

    def cmd_execute_action(self):
        mode = self.op_mode_cb.get()
        scope = self.scope_cb.get()

        if "Sweep" in mode:
            self.run_all_pattern_sweep_engine()
            return

        addr = self.ent_addr.get().strip()
        length = self.ent_len.get().strip()
        pat_idx = self.pat_cb.get().split(":")[0]
        custom_hex = self.get_custom_hex_val()

        if "Write Pattern" in mode:
            self.update_phase_status("[ Phase: WRITING... ]")
            if scope == "Entire Chip (2MB)":
                self.ops_log(f"[ACTION] Writing Pattern #{pat_idx} to ENTIRE CHIP (2MB)...")
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
            self.update_phase_status("[ State: IDLE ]")

        elif "Verify Pattern" in mode:
            self.update_phase_status("[ Phase: VERIFYING... ]")
            if scope == "Entire Chip (2MB)":
                self.ops_log(f"[ACTION] Verifying ENTIRE CHIP with Pattern #{pat_idx}...")
                t0 = time.time()
                resp = self.execute_cmd_with_progress(f"VERIFY_FULL_PAT:{pat_idx}:{custom_hex}", ["VERIFY:OK", "VERIFY:FAIL:"])
                t1 = time.time()
                if resp == "VERIFY:OK":
                    self.ops_log(f"[PASSED] 🟢 Full Chip Verification PASSED in {t1-t0:.2f}s!")
                    self.update_pin_health({2: "#34C759", 5: "#34C759"})
                elif resp and resp.startswith("VERIFY:FAIL:"):
                    parts = resp.split(":")
                    fail_addr_hex = parts[3]
                    self.ops_log(f"[FAILED] 🔴 Full Chip Verification FAILED! ({parts[2]} errors). First Mismatch @ {fail_addr_hex}")
                    self.update_pin_health({2: "#FF3B30", 5: "#FF3B30"})
                    self.auto_jump_to_hex_view(fail_addr_hex)
            else:
                self.ops_log(f"[ACTION] Verifying Pattern #{pat_idx} at {addr} (Len: {length} bytes)...")
                t0 = time.time()
                resp = self.execute_cmd_with_progress(f"VERIFY:{addr}:{length}:{pat_idx}:{custom_hex}", ["VERIFY:OK", "VERIFY:FAIL:"])
                t1 = time.time()
                if resp == "VERIFY:OK":
                    self.ops_log(f"[PASSED] 🟢 Verification PASSED (0 Errors) in {t1-t0:.3f}s!")
                    self.update_pin_health({2: "#34C759", 5: "#34C759"})
                elif resp and resp.startswith("VERIFY:FAIL:"):
                    parts = resp.split(":")
                    fail_addr_hex = parts[3]
                    self.ops_log(f"[FAILED] 🔴 Verification FAILED! ({parts[2]} errors). First Mismatch @ {fail_addr_hex}")
                    self.update_pin_health({2: "#FF3B30", 5: "#FF3B30"})
                    self.auto_jump_to_hex_view(fail_addr_hex)
            self.update_phase_status("[ State: IDLE ]")

        elif "Erase Sector" in mode:
            self.update_phase_status("[ Phase: ERASING... ]")
            self.ops_log(f"[ACTION] Erasing Sector at {addr}...")
            resp = self.execute_cmd_with_progress(f"ERASE:{addr}", ["ERASE:OK"])
            if resp == "ERASE:OK":
                self.ops_log(f"[SUCCESS] Sector {addr} Erased.")
            self.update_phase_status("[ State: IDLE ]")

        elif "Read Data" in mode:
            self.update_phase_status("[ Phase: READING... ]")
            resp = self.execute_cmd_with_progress(f"READ:{addr}:{length}", ["DATA:"])
            if resp and resp.startswith("DATA:"):
                self.ops_log(f"[READ @ {addr}] {resp[5:]}")
            self.update_phase_status("[ State: IDLE ]")

    def cmd_read_id(self):
        resp = self.execute_cmd_with_progress("READ_ID", ["ID:"])
        if resp and resp.startswith("ID:"):
            id_str = resp[3:].strip()
            self.lbl_jedec.config(text=f"JEDEC ID: {id_str}")
            if id_str in ["00 00 00", "FF FF FF"]:
                self.update_pin_health({1: "#FF3B30", 2: "#FF3B30", 4: "#FF3B30", 5: "#FF3B30", 6: "#FF3B30", 8: "#FF3B30"})
            else:
                self.update_pin_health({1: "#34C759", 2: "#34C759", 4: "#34C759", 5: "#34C759", 6: "#34C759", 7: "#34C759", 8: "#34C759"})
        else:
            messagebox.showerror("Error", "Failed to read JEDEC ID")
            self.update_pin_health({1: "#FF3B30", 2: "#FF3B30", 4: "#FF3B30", 5: "#FF3B30", 6: "#FF3B30", 8: "#FF3B30"})

    def cmd_unlock(self):
        resp = self.execute_cmd_with_progress("UNLOCK", ["UNLOCK:OK"])
        if resp == "UNLOCK:OK":
            messagebox.showinfo("Success", "Global Block Protection Unlocked!")
            self.update_pin_health({3: "#34C759"})
            self.cmd_read_regs()
        else:
            messagebox.showerror("Error", "Unlock failed")
            self.update_pin_health({3: "#FF3B30"})

    def cmd_read_regs(self):
        resp = self.execute_cmd_with_progress("READ_REGS", ["REGS:"])
        if resp and resp.startswith("REGS:"):
            parts = resp.split(":")
            stat_hex, cfg_hex = parts[1], parts[2]
            self.lbl_stat_reg.config(text=f"SR: 0x{stat_hex}")
            self.lbl_cfg_reg.config(text=f"CR: 0x{cfg_hex}")
        else:
            messagebox.showerror("Error", "Failed to read registers")

if __name__ == "__main__":
    root = tk.Tk()
    app = FlashProgrammerGUI(root)
    root.mainloop()