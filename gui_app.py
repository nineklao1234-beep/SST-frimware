import time
import csv
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
import serial
import serial.tools.list_ports

JEDEC_DB = {
    "BF 26 41": "Microchip / SST SST26VF016B (2MB / 16Mbit)",
    "BF 25 41": "Microchip / SST SST25VF016B (2MB / 16Mbit)",
    "EF 40 15": "Winbond W25Q16 (2MB / 16Mbit)",
    "EF 40 16": "Winbond W25Q32 (4MB / 32Mbit)",
    "C2 20 15": "Macronix MX25L1606E (2MB / 16Mbit)",
    "1C 20 15": "EON EN25Q16 (2MB / 16Mbit)",
    "20 80 15": "Micron N25Q032 (4MB / 32Mbit)"
}

class FlashProgrammerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("SST26VF016B Tester & Failure Analysis (FA) Programmer - ESP32")
        self.root.geometry("1140x840")
        self.root.resizable(False, False)
        
        self.ser = None
        self.target_hex_addr = 0x000000
        self.bytes_per_view = 256
        self.stop_requested = False
        self.is_syncing = False
        self.jedec_read_valid = False  # Track JEDEC ID check requirement
        self.is_unlocked = False       # Track Lock/Unlock Protection status

        # Multi-page Failure Analysis Data Store
        self.failed_pages = []            # Unique page start addresses
        self.failed_page_err_counts = {}  # Page Addr -> Error Count mapping
        self.failed_page_samples = {}     # Page Addr -> First Mismatch detail string
        self.tested_sectors = set()       # Set of tested sector indices
        self.current_fail_idx = -1
        self.selected_bitmap_sector = 0
        self.last_verified_pat_type = None
        self.last_verified_custom_hex = "AA"

        # Loop Test State Storage
        self.loop_running = False
        self.loop_pass_cnt = 0
        self.loop_fail_cnt = 0
        self.loop_current_iter = 0

        self.pin_colors = {i: "#777777" for i in range(1, 9)}
        self.bitmap_rects = {} # sector_idx -> canvas rect ID

        style = ttk.Style()
        style.theme_use('clam')

        # =========================================================================
        # TOP HEADER: SYSTEM CONNECTION, REFRESH & HARDWARE DIAGNOSTICS
        # =========================================================================
        top_bar = ttk.LabelFrame(root, text=" 🔌 Connection & SPI Settings ")
        top_bar.pack(fill="x", padx=10, pady=5)

        row0 = ttk.Frame(top_bar)
        row0.pack(fill="x", padx=5, pady=4)

        ttk.Label(row0, text="Port:").pack(side="left", padx=2)
        self.port_cb = ttk.Combobox(row0, width=10, state="readonly")
        self.port_cb.pack(side="left", padx=2)
        
        btn_refresh = ttk.Button(row0, text="Refresh Ports", command=self.refresh_ports)
        btn_refresh.pack(side="left", padx=2)

        self.btn_connect = ttk.Button(row0, text="Connect", command=self.toggle_connection)
        self.btn_connect.pack(side="left", padx=2)

        # UI Refresh & Safety Reset Button
        btn_reset_ui = ttk.Button(row0, text="🔄 Reset / Refresh UI", command=self.cmd_reset_ui_app)
        btn_reset_ui.pack(side="left", padx=(6, 2))

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
        self.progressbar = ttk.Progressbar(prog_frame, variable=self.progress_var, maximum=100, length=120)
        self.progressbar.pack(side="left", padx=2)

        self.lbl_progress = ttk.Label(prog_frame, text="0%", font=("Segoe UI", 9, "bold"), width=4)
        self.lbl_progress.pack(side="left", padx=2)

        btn_stop = tk.Button(prog_frame, text="⛔ STOP", command=self.cmd_stop, bg="#FF3B30", fg="white", font=("Segoe UI", 8, "bold"), relief="flat", padx=8)
        btn_stop.pack(side="left", padx=(5, 2))

        # Register Diagnostics Sub-bar
        row1 = ttk.Frame(top_bar)
        row1.pack(fill="x", padx=5, pady=(2, 4))

        ttk.Button(row1, text="Read JEDEC ID", command=self.cmd_read_id).pack(side="left", padx=2)
        ttk.Button(row1, text="Unlock Protection", command=self.cmd_unlock).pack(side="left", padx=2)
        ttk.Button(row1, text="Read Registers", command=self.cmd_read_regs).pack(side="left", padx=2)

        jedec_info_frame = ttk.Frame(row1)
        jedec_info_frame.pack(side="left", padx=(12, 6))

        self.lbl_jedec = ttk.Label(jedec_info_frame, text="JEDEC ID: -- -- --", font=("Consolas", 9, "bold"))
        self.lbl_jedec.pack(anchor="w")

        self.lbl_device = ttk.Label(jedec_info_frame, text="Device: --", font=("Segoe UI", 8, "italic"), foreground="#444444")
        self.lbl_device.pack(anchor="w")

        # Chip Lock / Unlock Status Indicator
        self.lbl_lock_status = ttk.Label(row1, text="🔒 Protection: LOCKED", font=("Segoe UI", 9, "bold"), foreground="#FF3B30")
        self.lbl_lock_status.pack(side="left", padx=(12, 6))

        self.lbl_stat_reg = ttk.Label(row1, text="SR(0x05): --", font=("Consolas", 9))
        self.lbl_stat_reg.pack(side="left", padx=6)

        self.lbl_cfg_reg = ttk.Label(row1, text="CR(0x35): --", font=("Consolas", 9))
        self.lbl_cfg_reg.pack(side="left", padx=6)

        self.lbl_phase = ttk.Label(row1, text="[ State: IDLE ]", font=("Consolas", 9, "bold"), foreground="#007ACC")
        self.lbl_phase.pack(side="right", padx=5)

        # =========================================================================
        # TAB SYSTEM ARCHITECTURE (5 Dedicated Tabs)
        # =========================================================================
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=5)

        self.tab_ops = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_ops, text=" ⚡ Memory Operations ")
        self.setup_tab_ops()

        self.tab_fa = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_fa, text=" 📊 Failure Analysis ")
        self.setup_tab_fa()

        self.tab_bitmap = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_bitmap, text=" 🗺️ Flash Bitmap ")
        self.setup_tab_bitmap()

        self.tab_hex = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_hex, text=" 🔍 Hex Viewer ")
        self.setup_tab_hex()

        self.tab_loop = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_loop, text=" 🔁 Targeted Loop & Stress Test ")
        self.setup_tab_loop()

        self.refresh_ports()

    def update_lock_ui(self, is_unlocked):
        """ Update Lock/Unlock Protection status label and pin color """
        self.is_unlocked = is_unlocked
        if is_unlocked:
            self.lbl_lock_status.config(text="🔓 Protection: UNLOCKED", foreground="#34C759")
            self.update_pin_health({3: "#34C759"})
        else:
            self.lbl_lock_status.config(text="🔒 Protection: LOCKED", foreground="#FF3B30")
            self.update_pin_health({3: "#FF3B30"})

    def check_jedec_requirement(self):
        """ Check if JEDEC ID has been read successfully before allowing operations """
        if not self.jedec_read_valid:
            messagebox.showwarning("⚠️ Warning: Must Read JEDEC ID First",
                                   "กรุณากดปุ่ม 'Read JEDEC ID' เพื่อตรวจสอบ IC ก่อนเริ่มทำรายการครับ!")
            return False
        return True

    def cmd_reset_ui_app(self):
        """ Safety reset button for clearing stuck states, serial buffers, and refreshing UI """
        self.stop_requested = False
        self.is_syncing = False
        self.jedec_read_valid = False
        self.loop_running = False
        self.update_lock_ui(False)
        if self.ser and self.ser.is_open:
            try:
                self.ser.reset_input_buffer()
                self.ser.reset_output_buffer()
            except Exception:
                pass
        self.update_progress(0)
        self.update_phase_status("[ State: IDLE ]")
        self.ops_log("[SYSTEM] 🔄 UI App & Serial Buffer refreshed and reset to IDLE. (Please Re-Read JEDEC ID)")

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
        self.loop_running = False
        if self.ser and self.ser.is_open:
            try:
                self.ser.write(b"STOP\n")
                self.ops_log("[SYSTEM] ⛔ Stop command sent to ESP32!")
                self.loop_log("[LOOP ENGINE] ⛔ Stop requested by user.")
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
            self.jedec_read_valid = False
            self.update_lock_ui(False)

    # Auto-Sync Handlers (Sector <-> Page <-> Hex Address)
    def on_sector_ops_changed(self, event=None):
        if self.is_syncing: return
        self.is_syncing = True
        val = self.ent_sector_ops.get().strip()
        if val.isdigit():
            sec_num = int(val)
            if 0 <= sec_num <= 511:
                addr = sec_num * 4096
                page_num = sec_num * 16
                self.ent_addr.delete(0, tk.END)
                self.ent_addr.insert(0, f"0x{addr:06X}")
                self.ent_page.delete(0, tk.END)
                self.ent_page.insert(0, str(page_num))
                self.sync_target_to_loop_tab("SECTOR", sec_num, page_num, addr, 4096)
        self.is_syncing = False

    def on_page_changed(self, event=None):
        if self.is_syncing: return
        self.is_syncing = True
        val = self.ent_page.get().strip()
        if val.isdigit():
            page_num = int(val)
            if 0 <= page_num <= 8191:
                addr = page_num * 256
                sec_num = page_num // 16
                self.ent_addr.delete(0, tk.END)
                self.ent_addr.insert(0, f"0x{addr:06X}")
                self.ent_sector_ops.delete(0, tk.END)
                self.ent_sector_ops.insert(0, str(sec_num))
                self.sync_target_to_loop_tab("PAGE", sec_num, page_num, addr, 256)
        self.is_syncing = False

    def on_addr_changed(self, event=None):
        if self.is_syncing: return
        self.is_syncing = True
        val = self.ent_addr.get().strip()
        try:
            addr = int(val, 16)
            if 0 <= addr < 0x200000:
                page_num = addr // 256
                sec_num = addr // 4096
                self.ent_page.delete(0, tk.END)
                self.ent_page.insert(0, str(page_num))
                self.ent_sector_ops.delete(0, tk.END)
                self.ent_sector_ops.insert(0, str(sec_num))
                self.sync_target_to_loop_tab("PAGE", sec_num, page_num, addr, 256)
        except ValueError:
            pass
        self.is_syncing = False

    # -------------------------------------------------------------------------
    # TAB 1: MEMORY OPERATIONS PANEL
    # -------------------------------------------------------------------------
    def setup_tab_ops(self):
        top_ops_container = ttk.Frame(self.tab_ops)
        top_ops_container.pack(fill="x", padx=10, pady=5)

        left_ops_frame = ttk.Frame(top_ops_container)
        left_ops_frame.pack(side="left", fill="both", expand=True)

        cfg_frame = ttk.LabelFrame(left_ops_frame, text=" ⚙️ Operation & Memory Targeting Parameters ")
        cfg_frame.pack(fill="x", padx=2, pady=2)

        ttk.Label(cfg_frame, text="Operation Mode:").grid(row=0, column=0, padx=5, pady=4, sticky="e")
        self.op_mode_cb = ttk.Combobox(cfg_frame, width=24, state="readonly")
        self.op_mode_cb['values'] = [
            "✍️ Write Pattern",
            "🔍 Verify Pattern",
            "🗑️ Erase Sector (4KB)",
            "📖 Read Data (Hex)",
            "🚀 All Patterns Sweep (Full Chip)"
        ]
        self.op_mode_cb.current(0)
        self.op_mode_cb.grid(row=0, column=1, padx=5, pady=4, sticky="w")
        self.op_mode_cb.bind("<<ComboboxSelected>>", self.update_param_states)

        ttk.Label(cfg_frame, text="Scope / Loop:").grid(row=0, column=2, padx=5, pady=4, sticky="e")
        self.scope_cb = ttk.Combobox(cfg_frame, width=16, state="readonly")
        self.scope_cb['values'] = ["Selected Range", "Entire Chip (2MB)", "Continuous Loop"]
        self.scope_cb.current(0)
        self.scope_cb.grid(row=0, column=3, padx=5, pady=4, sticky="w")
        self.scope_cb.bind("<<ComboboxSelected>>", self.update_param_states)

        ttk.Label(cfg_frame, text="Sector No.:").grid(row=1, column=0, padx=5, pady=4, sticky="e")
        sec_frame = ttk.Frame(cfg_frame)
        sec_frame.grid(row=1, column=1, padx=5, pady=4, sticky="w")
        self.ent_sector_ops = ttk.Entry(sec_frame, width=6)
        self.ent_sector_ops.insert(0, "0")
        self.ent_sector_ops.pack(side="left", padx=(0, 4))
        self.ent_sector_ops.bind("<KeyRelease>", self.on_sector_ops_changed)
        ttk.Label(sec_frame, text="(0-511 | Total 512)", font=("Segoe UI", 8, "italic"), foreground="#555555").pack(side="left")

        ttk.Label(cfg_frame, text="Page No.:").grid(row=1, column=2, padx=5, pady=4, sticky="e")
        page_input_frame = ttk.Frame(cfg_frame)
        page_input_frame.grid(row=1, column=3, padx=5, pady=4, sticky="w")
        self.ent_page = ttk.Entry(page_input_frame, width=6)
        self.ent_page.insert(0, "0")
        self.ent_page.pack(side="left", padx=(0, 4))
        self.ent_page.bind("<KeyRelease>", self.on_page_changed)
        ttk.Label(page_input_frame, text="(0-8191 | Total 8,192)", font=("Segoe UI", 8, "italic"), foreground="#555555").pack(side="left")

        ttk.Label(cfg_frame, text="Address (Hex):").grid(row=2, column=0, padx=5, pady=4, sticky="e")
        self.ent_addr = ttk.Entry(cfg_frame, width=12)
        self.ent_addr.insert(0, "0x000000")
        self.ent_addr.grid(row=2, column=1, padx=5, pady=4, sticky="w")
        self.ent_addr.bind("<KeyRelease>", self.on_addr_changed)

        ttk.Label(cfg_frame, text="Length (Bytes):").grid(row=2, column=2, padx=5, pady=4, sticky="e")
        self.ent_len = ttk.Entry(cfg_frame, width=12)
        self.ent_len.insert(0, "4096")
        self.ent_len.grid(row=2, column=3, padx=5, pady=4, sticky="w")

        ttk.Label(cfg_frame, text="Pattern Type:").grid(row=3, column=0, padx=5, pady=4, sticky="e")
        self.pat_cb = ttk.Combobox(cfg_frame, width=24, state="readonly")
        self.pat_cb['values'] = [
            "0: All 0xFF",
            "1: All 0x00",
            "2: All 0xAA",
            "3: All 0x55",
            "4: ✏️ Custom Hex",
            "5: 🎲 Pseudo-Random",
            "6: Alt 0xAA / 0x55",
            "7: Inc (0x00-0xFF)"
        ]
        self.pat_cb.current(0)
        self.pat_cb.grid(row=3, column=1, padx=5, pady=4, sticky="w")
        self.pat_cb.bind("<<ComboboxSelected>>", self.on_pattern_changed)

        ttk.Label(cfg_frame, text="Custom Hex (0x):").grid(row=3, column=2, padx=5, pady=4, sticky="e")
        self.ent_custom_hex = ttk.Entry(cfg_frame, width=12, state="disabled")
        self.ent_custom_hex.insert(0, "AA")
        self.ent_custom_hex.grid(row=3, column=3, padx=5, pady=4, sticky="w")

        act_frame = ttk.LabelFrame(left_ops_frame, text=" 🚀 Quick Command Launchers ")
        act_frame.pack(fill="x", padx=2, pady=4)

        btn_exec = ttk.Button(act_frame, text="▶ Execute Selected", command=self.cmd_execute_action)
        btn_exec.pack(side="left", padx=4, pady=6)

        btn_all_pat = ttk.Button(act_frame, text="🔥 Run All Patterns Sweep", command=self.run_all_pattern_sweep_engine)
        btn_all_pat.pack(side="left", padx=4, pady=6)

        btn_chip_erase = ttk.Button(act_frame, text="💥 Erase Full Chip", command=self.cmd_standalone_erase_chip)
        btn_chip_erase.pack(side="left", padx=4, pady=6)

        btn_send_loop_ops = ttk.Button(act_frame, text="⚡ Send Target to Loop Test", command=self.cmd_send_ops_target_to_loop)
        btn_send_loop_ops.pack(side="left", padx=4, pady=6)

        btn_clear = ttk.Button(act_frame, text="🧹 Clear Log", command=self.cmd_clear_log)
        btn_clear.pack(side="right", padx=4, pady=6)

        ic_frame = ttk.LabelFrame(top_ops_container, text=" 🩺 IC Pin Health Status ")
        ic_frame.pack(side="right", fill="y", padx=(8, 2), pady=2)

        self.ic_canvas = tk.Canvas(ic_frame, width=240, height=135, bg="#F0F0F0", highlightthickness=0)
        self.ic_canvas.pack(padx=8, pady=4)
        self.draw_ic_diagram()

        log_frame = ttk.LabelFrame(self.tab_ops, text=" 📜 Operation Output Log ")
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.txt_ops_log = scrolledtext.ScrolledText(log_frame, font=("Consolas", 9), state="disabled")
        self.txt_ops_log.pack(fill="both", expand=True, padx=5, pady=5)

    def update_param_states(self, event=None):
        """ Lock parameters (Sector, Page, Address, Length) when operating on Entire Chip / Sweep """
        mode = self.op_mode_cb.get()
        scope = self.scope_cb.get()

        is_full_chip = ("Entire Chip" in scope) or ("Sweep" in mode)

        if is_full_chip:
            self.ent_sector_ops.config(state="disabled")
            self.ent_page.config(state="disabled")
            self.ent_addr.config(state="disabled")
            self.ent_len.config(state="disabled")
        else:
            self.ent_sector_ops.config(state="normal")
            self.ent_page.config(state="normal")
            self.ent_addr.config(state="normal")
            self.ent_len.config(state="normal")

        if "Sweep" in mode:
            self.scope_cb.set("Entire Chip (2MB)")
            self.pat_cb.config(state="disabled")
        else:
            self.pat_cb.config(state="readonly")

    def on_pattern_changed(self, event=None):
        pat_idx = self.pat_cb.get().split(":")[0]
        if pat_idx == "4":
            self.ent_custom_hex.config(state="normal")
        else:
            self.ent_custom_hex.config(state="disabled")

    # -------------------------------------------------------------------------
    # TAB 2: DEDICATED FAILURE ANALYSIS TAB
    # -------------------------------------------------------------------------
    def setup_tab_fa(self):
        summary_frame = ttk.LabelFrame(self.tab_fa, text=" 📈 Verification Yield & Failure Overview ")
        summary_frame.pack(fill="x", padx=10, pady=5)

        sub_grid = ttk.Frame(summary_frame)
        sub_grid.pack(fill="x", padx=10, pady=5)

        self.lbl_fa_status = ttk.Label(sub_grid, text="STATUS: UNTESTED / NO ERRORS RECORDED", font=("Segoe UI", 10, "bold"), foreground="#007ACC")
        self.lbl_fa_status.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))

        ttk.Label(sub_grid, text="Failed Pages:").grid(row=1, column=0, sticky="e", padx=4)
        self.lbl_fa_failed_pages = ttk.Label(sub_grid, text="0 / 8,192 Pages", font=("Consolas", 9, "bold"))
        self.lbl_fa_failed_pages.grid(row=1, column=1, sticky="w", padx=4)

        ttk.Label(sub_grid, text="Impacted Sectors:").grid(row=1, column=2, sticky="e", padx=4)
        self.lbl_fa_failed_sectors = ttk.Label(sub_grid, text="0 / 512 Sectors", font=("Consolas", 9, "bold"))
        self.lbl_fa_failed_sectors.grid(row=1, column=3, sticky="w", padx=4)

        ttk.Label(sub_grid, text="Total Mismatch Bytes:").grid(row=2, column=0, sticky="e", padx=4)
        self.lbl_fa_total_bytes = ttk.Label(sub_grid, text="0 Bytes", font=("Consolas", 9, "bold"))
        self.lbl_fa_total_bytes.grid(row=2, column=1, sticky="w", padx=4)

        ttk.Label(sub_grid, text="Chip Defect Rate:").grid(row=2, column=2, sticky="e", padx=4)
        self.lbl_fa_defect_rate = ttk.Label(sub_grid, text="0.00 %", font=("Consolas", 9, "bold"), foreground="#34C759")
        self.lbl_fa_defect_rate.grid(row=2, column=3, sticky="w", padx=4)

        table_frame = ttk.LabelFrame(self.tab_fa, text=" 📋 Detailed Failing Pages Directory ")
        table_frame.pack(fill="both", expand=True, padx=10, pady=5)

        columns = ("idx", "sector", "page", "addr", "err_count", "sample")
        self.fa_tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")

        self.fa_tree.heading("idx", text="#", anchor="center")
        self.fa_tree.heading("sector", text="Sector No.", anchor="center")
        self.fa_tree.heading("page", text="Page No.", anchor="center")
        self.fa_tree.heading("addr", text="Start Hex Address", anchor="center")
        self.fa_tree.heading("err_count", text="Mismatch Bytes", anchor="center")
        self.fa_tree.heading("sample", text="First Error Sample Detail", anchor="w")

        self.fa_tree.column("idx", width=40, anchor="center")
        self.fa_tree.column("sector", width=100, anchor="center")
        self.fa_tree.column("page", width=100, anchor="center")
        self.fa_tree.column("addr", width=140, anchor="center")
        self.fa_tree.column("err_count", width=120, anchor="center")
        self.fa_tree.column("sample", width=340, anchor="w")

        tree_scroll_y = ttk.Scrollbar(table_frame, orient="vertical", command=self.fa_tree.yview)
        tree_scroll_x = ttk.Scrollbar(table_frame, orient="horizontal", command=self.fa_tree.xview)
        self.fa_tree.configure(yscrollcommand=tree_scroll_y.set, xscrollcommand=tree_scroll_x.set)

        tree_scroll_y.pack(side="right", fill="y")
        tree_scroll_x.pack(side="bottom", fill="x")
        self.fa_tree.pack(fill="both", expand=True)

        self.fa_tree.bind("<Double-1>", self.on_fa_tree_double_click)

        action_bar = ttk.Frame(self.tab_fa)
        action_bar.pack(fill="x", padx=10, pady=5)

        btn_inspect = ttk.Button(action_bar, text="🔍 Inspect Selected in Hex Viewer", command=self.cmd_inspect_fa_tree_selection)
        btn_inspect.pack(side="left", padx=4)

        btn_send_loop_fa = ttk.Button(action_bar, text="⚡ Send Page to Loop Test", command=self.cmd_send_fa_selection_to_loop)
        btn_send_loop_fa.pack(side="left", padx=4)

        btn_export = ttk.Button(action_bar, text="💾 Export FA Report (.csv)", command=self.cmd_export_fa_report)
        btn_export.pack(side="right", padx=4)

    def update_fa_table_data(self):
        for item in self.fa_tree.get_children():
            self.fa_tree.delete(item)

        if not self.failed_pages:
            self.lbl_fa_status.config(text="STATUS: VERIFICATION PASSED (0 ERRORS DETECTED)", foreground="#34C759")
            self.lbl_fa_failed_pages.config(text="0 / 8,192 Pages")
            self.lbl_fa_failed_sectors.config(text="0 / 512 Sectors")
            self.lbl_fa_total_bytes.config(text="0 Bytes")
            self.lbl_fa_defect_rate.config(text="0.00 %", foreground="#34C759")
            return

        total_failed_pages = len(self.failed_pages)
        impacted_sectors_set = set(p // 4096 for p in self.failed_pages)
        total_mismatch_bytes = sum(self.failed_page_err_counts.values())
        defect_rate = (total_mismatch_bytes / 2097152.0) * 100.0

        self.lbl_fa_status.config(text=f"STATUS: VERIFICATION FAILED ({total_failed_pages} FAILING PAGES DETECTED)", foreground="#FF3B30")
        self.lbl_fa_failed_pages.config(text=f"{total_failed_pages:,} / 8,192 Pages")
        self.lbl_fa_failed_sectors.config(text=f"{len(impacted_sectors_set):,} / 512 Sectors")
        self.lbl_fa_total_bytes.config(text=f"{total_mismatch_bytes:,} Bytes")
        self.lbl_fa_defect_rate.config(text=f"{defect_rate:.4f} %", foreground="#FF3B30")

        for idx, page_addr in enumerate(self.failed_pages):
            sec_num = page_addr // 4096
            page_num = page_addr // 256
            err_cnt = self.failed_page_err_counts.get(page_addr, 1)
            sample_str = self.failed_page_samples.get(page_addr, "Mismatch Detected")

            self.fa_tree.insert("", "end", iid=str(idx), values=(
                idx + 1,
                sec_num,
                page_num,
                f"0x{page_addr:06X}",
                f"{err_cnt} Bytes",
                sample_str
            ))

    def on_fa_tree_double_click(self, event=None):
        self.cmd_inspect_fa_tree_selection()

    def cmd_inspect_fa_tree_selection(self):
        selected = self.fa_tree.selection()
        if not selected:
            messagebox.showwarning("Notice", "Please select a failing page row from the table first!")
            return
        
        idx = int(selected[0])
        if 0 <= idx < len(self.failed_pages):
            target_addr = self.failed_pages[idx]
            self.target_hex_addr = target_addr
            self.ent_hex_jump.delete(0, tk.END)
            self.ent_hex_jump.insert(0, f"0x{self.target_hex_addr:06X}")
            self.update_hex_view_labels()
            self.notebook.select(3) # Switch to Tab 4: Hex Viewer
            self.load_hex_view_page()

    def cmd_send_fa_selection_to_loop(self):
        selected = self.fa_tree.selection()
        if not selected:
            messagebox.showwarning("Notice", "Please select a failing page row from the table first!")
            return
        idx = int(selected[0])
        if 0 <= idx < len(self.failed_pages):
            target_addr = self.failed_pages[idx]
            sec_num = target_addr // 4096
            page_num = target_addr // 256
            self.sync_target_to_loop_tab("PAGE", sec_num, page_num, target_addr, 256)
            self.notebook.select(4) # Switch to Tab 5: Loop Test

    def cmd_export_fa_report(self):
        if not self.failed_pages:
            messagebox.showinfo("Notice", "No failure records available to export.")
            return

        filepath = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV UTF-8", "*.csv"), ("All Files", "*.*")],
            title="Export Failure Analysis Report"
        )
        if not filepath:
            return

        try:
            with open(filepath, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["Index", "Sector_No", "Page_No", "Hex_Address", "Mismatch_Bytes", "Error_Details"])
                for idx, page_addr in enumerate(self.failed_pages):
                    sec_num = page_addr // 4096
                    page_num = page_addr // 256
                    err_cnt = self.failed_page_err_counts.get(page_addr, 1)
                    sample_str = self.failed_page_samples.get(page_addr, "")
                    writer.writerow([idx + 1, sec_num, page_num, f"0x{page_addr:06X}", err_cnt, sample_str])
            messagebox.showinfo("Success", f"FA Report exported successfully to:\n{filepath}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to export report: {e}")

    # -------------------------------------------------------------------------
    # TAB 3: DEDICATED FLASH BITMAP TAB
    # -------------------------------------------------------------------------
        def setup_tab_bitmap(self):
        header_frame = ttk.LabelFrame(self.tab_bitmap, text=" 🗺️ 512 Sector Flash Memory Block Map (4KB / Sector) ")
        header_frame.pack(fill="x", padx=10, pady=5)

        info_row = ttk.Frame(header_frame)
        info_row.pack(fill="x", padx=5, pady=4)

        ttk.Label(info_row, text="Legend: ", font=("Segoe UI", 9, "bold")).pack(side="left", padx=2)

        c_pass = tk.Canvas(info_row, width=14, height=14, highlightthickness=0)
        c_pass.pack(side="left", padx=2)
        c_pass.create_rectangle(0, 0, 14, 14, fill="#34C759", outline="#1D8339")
        ttk.Label(info_row, text="Pass / OK  ").pack(side="left")

        c_fail = tk.Canvas(info_row, width=14, height=14, highlightthickness=0)
        c_fail.pack(side="left", padx=2)
        c_fail.create_rectangle(0, 0, 14, 14, fill="#FF3B30", outline="#B30000")
        ttk.Label(info_row, text="Fail Mismatch  ").pack(side="left")

        c_untest = tk.Canvas(info_row, width=14, height=14, highlightthickness=0)
        c_untest.pack(side="left", padx=2)
        c_untest.create_rectangle(0, 0, 14, 14, fill="#333333", outline="#1A1A1A")
        ttk.Label(info_row, text="Untested / Blank  ").pack(side="left")

        btn_sync_map = ttk.Button(info_row, text="🔄 Refresh Grid Render", command=self.render_bitmap_grid)
        btn_sync_map.pack(side="left", padx=10)

        self.lbl_bitmap_selected = ttk.Label(info_row, text="Selected: Sector 0 (0x000000 - 0x000FFF)", font=("Consolas", 9, "bold"), foreground="#007ACC")
        self.lbl_bitmap_selected.pack(side="right", padx=5)

        canvas_frame = ttk.LabelFrame(self.tab_bitmap, text=" Sector Memory Matrix (32 Cols x 16 Rows) ")
        canvas_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.bitmap_canvas = tk.Canvas(canvas_frame, bg="#1E1E1E", highlightthickness=0)
        
        bitmap_scroll_y = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.bitmap_canvas.yview)
        bitmap_scroll_x = ttk.Scrollbar(canvas_frame, orient="horizontal", command=self.bitmap_canvas.xview)
        self.bitmap_canvas.configure(yscrollcommand=bitmap_scroll_y.set, xscrollcommand=bitmap_scroll_x.set)

        bitmap_scroll_y.pack(side="right", fill="y")
        bitmap_scroll_x.pack(side="bottom", fill="x")
        self.bitmap_canvas.pack(fill="both", expand=True, padx=5, pady=5)

        self.bitmap_canvas.bind("<Button-1>", self.on_bitmap_click)
        self.bitmap_canvas.bind("<Double-1>", self.on_bitmap_double_click)

        bottom_bar = ttk.Frame(self.tab_bitmap)
        bottom_bar.pack(fill="x", padx=10, pady=5)

        btn_inspect_map = ttk.Button(bottom_bar, text="🔍 Inspect Selected Sector in Hex Viewer", command=self.cmd_inspect_bitmap_selection)
        btn_inspect_map.pack(side="left", padx=4)

        btn_send_loop_bmp = ttk.Button(bottom_bar, text="⚡ Send Sector to Loop Test", command=self.cmd_send_bitmap_selection_to_loop)
        btn_send_loop_bmp.pack(side="left", padx=4)

        ttk.Label(bottom_bar, text="💡 Tip: Double-click any sector box to jump directly to Hex Viewer!", font=("Segoe UI", 8, "italic"), foreground="#666666").pack(side="right", padx=5)

        self.render_bitmap_grid()

        def render_bitmap_grid(self):
        """ High performance fast Canvas drawing for 512 Sector blocks """
        self.bitmap_canvas.delete("all")
        self.bitmap_rects.clear()

        cols = 32
        rows = 16
        box_w = 28
        box_h = 24
        pad_x = 3
        pad_y = 4
        start_x = 10
        start_y = 10

        impacted_sectors = set(p // 4096 for p in self.failed_pages)

        for sec in range(512):
            r = sec // cols
            c = sec % cols

            x1 = start_x + c * (box_w + pad_x)
            y1 = start_y + r * (box_h + pad_y)
            x2 = x1 + box_w
            y2 = y1 + box_h

            if sec in impacted_sectors:
                fill_color = "#FF3B30" # Red
                outline_color = "#B30000"
            elif sec in self.tested_sectors:
                fill_color = "#34C759" # Green
                outline_color = "#1D8339"
            else:
                fill_color = "#333333" # Dark Grey
                outline_color = "#555555"

            if sec == self.selected_bitmap_sector:
                outline_color = "#FFCC00" # Yellow Highlight border

            rect_id = self.bitmap_canvas.create_rectangle(x1, y1, x2, y2, fill=fill_color, outline=outline_color, width=2 if sec == self.selected_bitmap_sector else 1)
            
            text_color = "#FFFFFF" if sec in impacted_sectors or sec in self.tested_sectors or sec == self.selected_bitmap_sector else "#888888"
            self.bitmap_canvas.create_text(x1 + box_w//2, y1 + box_h//2, text=str(sec), fill=text_color, font=("Consolas", 7, "bold"))
            
            self.bitmap_rects[sec] = rect_id

        total_w = start_x + cols * (box_w + pad_x) + 10
        total_h = start_y + rows * (box_h + pad_y) + 10
        self.bitmap_canvas.config(scrollregion=(0, 0, total_w, total_h))

        def on_bitmap_click(self, event):
        cols = 32
        box_w = 28
        box_h = 24
        pad_x = 3
        pad_y = 4
        start_x = 10
        start_y = 10

        click_x = self.bitmap_canvas.canvasx(event.x)
        click_y = self.bitmap_canvas.canvasy(event.y)

        c = int((click_x - start_x) // (box_w + pad_x))
        r = int((click_y - start_y) // (box_h + pad_y))

        if 0 <= c < cols and 0 <= r < 16:
            sec = r * cols + c
            if 0 <= sec <= 511:
                self.selected_bitmap_sector = sec
                self.target_hex_addr = sec * 4096
                
                start_addr = sec * 4096
                end_addr = start_addr + 4095
                impacted_sectors = set(p // 4096 for p in self.failed_pages)
                
                if sec in impacted_sectors:
                    st_str = "🔴 FAILED (MISMATCH)"
                elif sec in self.tested_sectors:
                    st_str = "🟢 PASSED"
                else:
                    st_str = "⬜ UNTESTED"

                self.lbl_bitmap_selected.config(text=f"Sector {sec} (0x{start_addr:06X} - 0x{end_addr:06X}) | Status: {st_str}")
                self.render_bitmap_grid()
                self.sync_target_to_loop_tab("SECTOR", sec, sec * 16, start_addr, 4096)

    def on_bitmap_double_click(self, event):
        self.on_bitmap_click(event)
        self.cmd_inspect_bitmap_selection()

    def cmd_inspect_bitmap_selection(self):
        if not self.check_jedec_requirement(): return
        sec = self.selected_bitmap_sector
        self.target_hex_addr = sec * 4096
        self.ent_hex_jump.delete(0, tk.END)
        self.ent_hex_jump.insert(0, f"0x{self.target_hex_addr:06X}")
        self.update_hex_view_labels()
        self.notebook.select(3) # Switch to Tab 4: Hex Viewer
        self.load_hex_view_page()

    def cmd_send_bitmap_selection_to_loop(self):
        sec = self.selected_bitmap_sector
        start_addr = sec * 4096
        self.sync_target_to_loop_tab("SECTOR", sec, sec * 16, start_addr, 4096)
        self.notebook.select(4) # Switch to Tab 5: Loop Test

    # -------------------------------------------------------------------------
    # TAB 4: DEDICATED HEX VIEWER
    # -------------------------------------------------------------------------
    def setup_tab_hex(self):
        nav_frame = ttk.LabelFrame(self.tab_hex, text=" Navigation & View Granularity ")
        nav_frame.pack(fill="x", padx=10, pady=4)

        ttk.Label(nav_frame, text="View Size:").grid(row=0, column=0, padx=5, pady=4, sticky="e")
        self.hex_view_size_cb = ttk.Combobox(nav_frame, width=18, state="readonly")
        self.hex_view_size_cb['values'] = ["Page (256 Bytes)", "Sector (4KB / 4096B)"]
        self.hex_view_size_cb.current(0)
        self.hex_view_size_cb.grid(row=0, column=1, padx=5, pady=4)
        self.hex_view_size_cb.bind("<<ComboboxSelected>>", self.on_hex_view_size_changed)

        ttk.Label(nav_frame, text="Sector No.:").grid(row=0, column=2, padx=5, pady=4, sticky="e")
        self.ent_hex_sector = ttk.Entry(nav_frame, width=8)
        self.ent_hex_sector.insert(0, "0")
        self.ent_hex_sector.grid(row=0, column=3, padx=5, pady=4)
        self.ent_hex_sector.bind("<KeyRelease>", self.on_hex_sector_input)

        ttk.Label(nav_frame, text="Address (Hex):").grid(row=0, column=4, padx=5, pady=4, sticky="e")
        self.ent_hex_jump = ttk.Entry(nav_frame, width=10)
        self.ent_hex_jump.insert(0, "0x000000")
        self.ent_hex_jump.grid(row=0, column=5, padx=5, pady=4)

        ttk.Button(nav_frame, text="◀ Prev Block", command=self.cmd_hex_prev_block).grid(row=0, column=6, padx=4, pady=4)
        ttk.Button(nav_frame, text="Next Block ▶", command=self.cmd_hex_next_block).grid(row=0, column=7, padx=4, pady=4)
        
        btn_load_hex = ttk.Button(nav_frame, text="📥 Load Hex Data", command=self.load_hex_view_page)
        btn_load_hex.grid(row=0, column=8, padx=6, pady=4)

        self.lbl_hex_page_info = ttk.Label(nav_frame, text="Range: 0x000000 - 0x0000FF", font=("Consolas", 9, "bold"))
        self.lbl_hex_page_info.grid(row=0, column=9, padx=10, pady=4)

        fail_nav_frame = ttk.LabelFrame(self.tab_hex, text=" 🔴 Verification Failure Quick Dropdown ")
        fail_nav_frame.pack(fill="x", padx=10, pady=3)

        ttk.Label(fail_nav_frame, text="Failed Pages List:").pack(side="left", padx=5, pady=4)
        self.fail_nav_cb = ttk.Combobox(fail_nav_frame, width=42, state="readonly")
        self.fail_nav_cb.pack(side="left", padx=5, pady=4)
        self.fail_nav_cb.bind("<<ComboboxSelected>>", self.on_fail_dropdown_selected)

        ttk.Button(fail_nav_frame, text="◀ Prev Fail", command=self.cmd_prev_fail).pack(side="left", padx=4, pady=4)
        ttk.Button(fail_nav_frame, text="Next Fail ▶", command=self.cmd_next_fail).pack(side="left", padx=4, pady=4)

        self.lbl_fail_summary = ttk.Label(fail_nav_frame, text="No Mismatches Recorded", font=("Segoe UI", 9, "bold"), foreground="#34C759")
        self.lbl_fail_summary.pack(side="right", padx=10, pady=4)

        view_frame = ttk.LabelFrame(self.tab_hex, text=" Hex Data Memory Array ")
        view_frame.pack(fill="both", expand=True, padx=10, pady=4)

        self.txt_hex_display = scrolledtext.ScrolledText(view_frame, font=("Consolas", 10), state="disabled", bg="#1E1E1E", fg="#D4D4D4")
        self.txt_hex_display.pack(fill="both", expand=True, padx=5, pady=5)
        self.txt_hex_display.tag_config("mismatch", background="#FF3B30", foreground="#FFFFFF")

        # Bottom Action Bar
        bottom_bar = ttk.Frame(self.tab_hex)
        bottom_bar.pack(fill="x", padx=10, pady=(2, 6))

        btn_send_hex_loop = ttk.Button(bottom_bar, text="⚡ Send View to Loop Test", command=self.cmd_send_hex_view_to_loop)
        btn_send_hex_loop.pack(side="left", padx=4)

    def on_hex_view_size_changed(self, event=None):
        if "Sector" in self.hex_view_size_cb.get():
            self.bytes_per_view = 4096
        else:
            self.bytes_per_view = 256
        self.update_hex_view_labels()

    def on_hex_sector_input(self, event=None):
        val = self.ent_hex_sector.get().strip()
        if val.isdigit():
            sec_num = int(val)
            if 0 <= sec_num <= 511:
                self.target_hex_addr = sec_num * 4096
                self.ent_hex_jump.delete(0, tk.END)
                self.ent_hex_jump.insert(0, f"0x{self.target_hex_addr:06X}")
                self.update_hex_view_labels()

    def update_hex_view_labels(self):
        sec_num = self.target_hex_addr // 4096
        self.ent_hex_sector.delete(0, tk.END)
        self.ent_hex_sector.insert(0, str(sec_num))
        end_addr = self.target_hex_addr + self.bytes_per_view - 1
        self.lbl_hex_page_info.config(text=f"Range: 0x{self.target_hex_addr:06X} - 0x{end_addr:06X}")

    def cmd_hex_prev_block(self):
        if self.target_hex_addr >= self.bytes_per_view:
            self.target_hex_addr -= self.bytes_per_view
            self.ent_hex_jump.delete(0, tk.END)
            self.ent_hex_jump.insert(0, f"0x{self.target_hex_addr:06X}")
            self.update_hex_view_labels()

    def cmd_hex_next_block(self):
        if self.target_hex_addr + self.bytes_per_view < 0x200000:
            self.target_hex_addr += self.bytes_per_view
            self.ent_hex_jump.delete(0, tk.END)
            self.ent_hex_jump.insert(0, f"0x{self.target_hex_addr:06X}")
            self.update_hex_view_labels()

    def cmd_send_hex_view_to_loop(self):
        addr = self.target_hex_addr
        sec_num = addr // 4096
        page_num = addr // 256
        scope_type = "SECTOR" if self.bytes_per_view == 4096 else "PAGE"
        self.sync_target_to_loop_tab(scope_type, sec_num, page_num, addr, self.bytes_per_view)
        self.notebook.select(4) # Switch to Tab 5: Loop Test

    def update_fail_navigation_ui(self):
        if not self.failed_pages:
            self.fail_nav_cb['values'] = []
            self.fail_nav_cb.set("")
            self.lbl_fail_summary.config(text="No Mismatches Recorded", foreground="#34C759")
            return

        total_pages = len(self.failed_pages)
        combo_vals = []
        for idx, page_addr in enumerate(self.failed_pages):
            page_num = page_addr // 256
            err_cnt = self.failed_page_err_counts.get(page_addr, 1)
            combo_vals.append(f"[{idx+1}/{total_pages}] Page {page_num} (0x{page_addr:06X}) - {err_cnt} Errors")

        self.fail_nav_cb['values'] = combo_vals
        if self.current_fail_idx < 0: self.current_fail_idx = 0
        self.fail_nav_cb.current(self.current_fail_idx)
        self.lbl_fail_summary.config(text=f"Total Failing Pages: {total_pages}", foreground="#FF3B30")

    def jump_to_fail_index(self, idx):
        if not self.check_jedec_requirement(): return
        if 0 <= idx < len(self.failed_pages):
            self.current_fail_idx = idx
            target_addr = self.failed_pages[idx]
            self.target_hex_addr = target_addr
            self.ent_hex_jump.delete(0, tk.END)
            self.ent_hex_jump.insert(0, f"0x{self.target_hex_addr:06X}")
            self.update_hex_view_labels()
            self.update_fail_navigation_ui()
            self.load_hex_view_page()

    def on_fail_dropdown_selected(self, event=None):
        idx = self.fail_nav_cb.current()
        if idx >= 0:
            self.jump_to_fail_index(idx)

    def cmd_prev_fail(self):
        if self.failed_pages and self.current_fail_idx > 0:
            self.jump_to_fail_index(self.current_fail_idx - 1)

    def cmd_next_fail(self):
        if self.failed_pages and self.current_fail_idx < len(self.failed_pages) - 1:
            self.jump_to_fail_index(self.current_fail_idx + 1)

    def get_expected_byte(self, offset, pat_type, custom_hex_str="AA"):
        if pat_type == 0: return 0xFF
        elif pat_type == 1: return 0x00
        elif pat_type == 2: return 0xAA
        elif pat_type == 3: return 0x55
        elif pat_type == 4:
            try: return int(custom_hex_str, 16) & 0xFF
            except ValueError: return 0xAA
        elif pat_type == 5:
            hash_val = (offset ^ 0x5BF03635) & 0xFFFFFFFF
            hash_val = ((hash_val ^ (hash_val >> 16)) * 0x45d9f3b) & 0xFFFFFFFF
            hash_val = ((hash_val ^ (hash_val >> 16)) * 0x45d9f3b) & 0xFFFFFFFF
            return (hash_val ^ (hash_val >> 16)) & 0xFF
        elif pat_type == 6:
            return 0xAA if (offset % 2 == 0) else 0x55
        elif pat_type == 7:
            return offset & 0xFF
        return 0xFF

    def load_hex_view_page(self):
        if not self.check_jedec_requirement(): return
        try:
            addr = int(self.ent_hex_jump.get().strip(), 16)
            addr = addr & ~0xF
            if 0 <= addr < 0x200000:
                self.target_hex_addr = addr
                self.update_hex_view_labels()
            else:
                messagebox.showerror("Error", "Address out of range (0x000000 - 0x1FFFFF)")
                return
        except ValueError:
            messagebox.showerror("Error", "Invalid Hex Address format")
            return

        self.update_phase_status("[ Phase: LOADING HEX DATA... ]")
        addr_str = f"0x{self.target_hex_addr:06X}"
        resp = self.execute_cmd_with_progress(f"READ:{addr_str}:{self.bytes_per_view}", ["DATA:"])
        if not resp or not resp.startswith("DATA:"):
            self.update_phase_status("[ State: IDLE ]")
            return

        raw_hex_list = resp[5:].strip().split(" ")
        try:
            raw_bytes = bytes([int(x, 16) for x in raw_hex_list])
        except ValueError:
            self.update_phase_status("[ State: IDLE ]")
            return

        self.txt_hex_display.config(state="normal")
        self.txt_hex_display.delete('1.0', tk.END)

        header = "Offset    00 01 02 03 04 05 06 07  08 09 0A 0B 0C 0D 0E 0F  Decoded ASCII\n"
        header += "-" * 77 + "\n"
        self.txt_hex_display.insert(tk.END, header)

        for i in range(0, len(raw_bytes), 16):
            chunk = raw_bytes[i:i+16]
            row_addr = f"{self.target_hex_addr + i:08X}"
            
            self.txt_hex_display.insert(tk.END, f"{row_addr}  ")

            for j in range(len(chunk)):
                b = chunk[j]
                byte_offset = i + j
                abs_addr = self.target_hex_addr + byte_offset
                
                is_mismatch = False
                if self.last_verified_pat_type is not None:
                    exp_b = self.get_expected_byte(abs_addr, self.last_verified_pat_type, self.last_verified_custom_hex)
                    if b != exp_b:
                        is_mismatch = True

                hex_str = f"{b:02X}"
                if is_mismatch:
                    self.txt_hex_display.insert(tk.END, hex_str, "mismatch")
                else:
                    self.txt_hex_display.insert(tk.END, hex_str)

                if j == 7:
                    self.txt_hex_display.insert(tk.END, "  ")
                elif j < 15:
                    self.txt_hex_display.insert(tk.END, " ")

            if len(chunk) < 16:
                rem = 16 - len(chunk)
                pad_space = rem * 3 + (1 if len(chunk) <= 8 else 0)
                self.txt_hex_display.insert(tk.END, " " * pad_space)

            self.txt_hex_display.insert(tk.END, "  |")

            for j in range(len(chunk)):
                b = chunk[j]
                abs_addr = self.target_hex_addr + i + j
                char_str = chr(b) if 32 <= b <= 126 else "."
                
                is_mismatch = False
                if self.last_verified_pat_type is not None:
                    exp_b = self.get_expected_byte(abs_addr, self.last_verified_pat_type, self.last_verified_custom_hex)
                    if b != exp_b:
                        is_mismatch = True

                if is_mismatch:
                    self.txt_hex_display.insert(tk.END, char_str, "mismatch")
                else:
                    self.txt_hex_display.insert(tk.END, char_str)

            self.txt_hex_display.insert(tk.END, "|\n")

        self.txt_hex_display.config(state="disabled")
        self.update_phase_status("[ State: IDLE ]")

    # -------------------------------------------------------------------------
    # TAB 5: DEDICATED TARGETED LOOP & STRESS TEST TAB (NEW FEATURE)
    # -------------------------------------------------------------------------
    def setup_tab_loop(self):
        # 1. Target Context Banner
        target_frame = ttk.LabelFrame(self.tab_loop, text=" 🎯 Active Target Context ")
        target_frame.pack(fill="x", padx=10, pady=4)

        t_row = ttk.Frame(target_frame)
        t_row.pack(fill="x", padx=6, pady=4)

        ttk.Label(t_row, text="Scope Target:").grid(row=0, column=0, padx=4, pady=2, sticky="e")
        self.loop_scope_cb = ttk.Combobox(t_row, width=16, state="readonly")
        self.loop_scope_cb['values'] = ["Page (256B)", "Sector (4KB)", "Entire Chip (2MB)"]
        self.loop_scope_cb.current(0)
        self.loop_scope_cb.grid(row=0, column=1, padx=4, pady=2, sticky="w")
        self.loop_scope_cb.bind("<<ComboboxSelected>>", self.on_loop_scope_changed)

        ttk.Label(t_row, text="Sector No.:").grid(row=0, column=2, padx=4, pady=2, sticky="e")
        self.ent_loop_sector = ttk.Entry(t_row, width=6)
        self.ent_loop_sector.insert(0, "0")
        self.ent_loop_sector.grid(row=0, column=3, padx=4, pady=2, sticky="w")
        self.ent_loop_sector.bind("<KeyRelease>", self.on_loop_sector_input)

        ttk.Label(t_row, text="Page No.:").grid(row=0, column=4, padx=4, pady=2, sticky="e")
        self.ent_loop_page = ttk.Entry(t_row, width=6)
        self.ent_loop_page.insert(0, "0")
        self.ent_loop_page.grid(row=0, column=5, padx=4, pady=2, sticky="w")
        self.ent_loop_page.bind("<KeyRelease>", self.on_loop_page_input)

        ttk.Label(t_row, text="Address (Hex):").grid(row=0, column=6, padx=4, pady=2, sticky="e")
        self.ent_loop_addr = ttk.Entry(t_row, width=10)
        self.ent_loop_addr.insert(0, "0x000000")
        self.ent_loop_addr.grid(row=0, column=7, padx=4, pady=2, sticky="w")
        self.ent_loop_addr.bind("<KeyRelease>", self.on_loop_addr_input)

        ttk.Label(t_row, text="Length:").grid(row=0, column=8, padx=4, pady=2, sticky="e")
        self.ent_loop_len = ttk.Entry(t_row, width=8)
        self.ent_loop_len.insert(0, "256")
        self.ent_loop_len.grid(row=0, column=9, padx=4, pady=2, sticky="w")

        self.lbl_target_summary = ttk.Label(target_frame, text="🎯 TARGET: Page 0 | Sector 0 | Addr Range: 0x000000 - 0x0000FF (256 Bytes)", font=("Consolas", 9, "bold"), foreground="#007ACC")
        self.lbl_target_summary.pack(anchor="w", padx=10, pady=(2, 6))

        # 2. Configuration & Conditions
        cfg_frame = ttk.LabelFrame(self.tab_loop, text=" ⚙️ Loop Execution Parameters & Stress Options ")
        cfg_frame.pack(fill="x", padx=10, pady=4)

        c_row1 = ttk.Frame(cfg_frame)
        c_row1.pack(fill="x", padx=6, pady=4)

        ttk.Label(c_row1, text="Stress Operation:").grid(row=0, column=0, padx=4, pady=2, sticky="e")
        self.loop_op_cb = ttk.Combobox(c_row1, width=22, state="readonly")
        self.loop_op_cb['values'] = [
            "🔍 Verify Pattern",
            "✍️ Write Pattern",
            "🔄 Erase -> Write -> Verify",
            "📖 Read Test"
        ]
        self.loop_op_cb.current(0)
        self.loop_op_cb.grid(row=0, column=1, padx=4, pady=2, sticky="w")

        ttk.Label(c_row1, text="Pattern Type:").grid(row=0, column=2, padx=4, pady=2, sticky="e")
        self.loop_pat_cb = ttk.Combobox(c_row1, width=20, state="readonly")
        self.loop_pat_cb['values'] = [
            "0: All 0xFF",
            "1: All 0x00",
            "2: All 0xAA",
            "3: All 0x55",
            "4: Custom Hex",
            "5: Pseudo-Random",
            "6: Alt 0xAA/0x55",
            "7: Inc (0x00-0xFF)"
        ]
        self.loop_pat_cb.current(2) # Default 0xAA
        self.loop_pat_cb.grid(row=0, column=3, padx=4, pady=2, sticky="w")

        ttk.Label(c_row1, text="Custom Hex (0x):").grid(row=0, column=4, padx=4, pady=2, sticky="e")
        self.ent_loop_custom_hex = ttk.Entry(c_row1, width=6)
        self.ent_loop_custom_hex.insert(0, "AA")
        self.ent_loop_custom_hex.grid(row=0, column=5, padx=4, pady=2, sticky="w")

        c_row2 = ttk.Frame(cfg_frame)
        c_row2.pack(fill="x", padx=6, pady=4)

        ttk.Label(c_row2, text="Loop Mode:").pack(side="left", padx=4)
        self.loop_mode_var = tk.StringVar(value="CONTINUOUS")

        rb_cont = ttk.Radiobutton(c_row2, text="Continuous (วนไม่จำกัด)", value="CONTINUOUS", variable=self.loop_mode_var, command=self.on_loop_mode_radio_changed)
        rb_cont.pack(side="left", padx=4)

        rb_count = ttk.Radiobutton(c_row2, text="Fixed Count (กำหนดรอบ):", value="COUNT", variable=self.loop_mode_var, command=self.on_loop_mode_radio_changed)
        rb_count.pack(side="left", padx=4)

        self.ent_loop_max_count = ttk.Entry(c_row2, width=8, state="disabled")
        self.ent_loop_max_count.insert(0, "1000")
        self.ent_loop_max_count.pack(side="left", padx=2)

        ttk.Label(c_row2, text="Interval Delay (ms):").pack(side="left", padx=(12, 2))
        self.ent_loop_delay = ttk.Entry(c_row2, width=6)
        self.ent_loop_delay.insert(0, "50")
        self.ent_loop_delay.pack(side="left", padx=2)

        self.stop_on_fail_var = tk.BooleanVar(value=True)
        chk_stop_fail = ttk.Checkbutton(c_row2, text="🛑 Stop Immediately on First Failure", variable=self.stop_on_fail_var)
        chk_stop_fail.pack(side="left", padx=(16, 4))

        # 3. Live Statistics Dashboard
        stat_frame = ttk.LabelFrame(self.tab_loop, text=" 📊 Real-time Stress Test Statistics ")
        stat_frame.pack(fill="x", padx=10, pady=4)

        s_grid = ttk.Frame(stat_frame)
        s_grid.pack(fill="x", padx=10, pady=6)

        ttk.Label(s_grid, text="Current Loop Iteration:").grid(row=0, column=0, sticky="e", padx=4)
        self.lbl_stat_iter = ttk.Label(s_grid, text="0 / ∞", font=("Consolas", 11, "bold"), foreground="#007ACC")
        self.lbl_stat_iter.grid(row=0, column=1, sticky="w", padx=4)

        ttk.Label(s_grid, text="Passed Cycles:").grid(row=0, column=2, sticky="e", padx=4)
        self.lbl_stat_pass = ttk.Label(s_grid, text="0", font=("Consolas", 11, "bold"), foreground="#34C759")
        self.lbl_stat_pass.grid(row=0, column=3, sticky="w", padx=4)

        ttk.Label(s_grid, text="Failed Cycles:").grid(row=0, column=4, sticky="e", padx=4)
        self.lbl_stat_fail = ttk.Label(s_grid, text="0", font=("Consolas", 11, "bold"), foreground="#FF3B30")
        self.lbl_stat_fail.grid(row=0, column=5, sticky="w", padx=4)

        # 4. Action Buttons
        act_row = ttk.Frame(self.tab_loop)
        act_row.pack(fill="x", padx=10, pady=4)

        self.btn_run_loop = ttk.Button(act_row, text="▶ START TARGET LOOP", command=self.cmd_start_target_loop)
        self.btn_run_loop.pack(side="left", padx=4)

        self.btn_stop_loop = ttk.Button(act_row, text="⛔ STOP LOOP", command=self.cmd_stop)
        self.btn_stop_loop.pack(side="left", padx=4)

        btn_reset_stats = ttk.Button(act_row, text="🔄 Reset Statistics", command=self.reset_loop_stats)
        btn_reset_stats.pack(side="left", padx=4)

        btn_clear_loop_log = ttk.Button(act_row, text="🧹 Clear Event Log", command=self.cmd_clear_loop_log)
        btn_clear_loop_log.pack(side="right", padx=4)

        # 5. Loop History & Error Log
        log_frame = ttk.LabelFrame(self.tab_loop, text=" 📜 Loop Event & Error Log History ")
        log_frame.pack(fill="both", expand=True, padx=10, pady=4)

        self.txt_loop_log = scrolledtext.ScrolledText(log_frame, font=("Consolas", 9), state="disabled", bg="#1E1E1E", fg="#D4D4D4")
        self.txt_loop_log.pack(fill="both", expand=True, padx=5, pady=5)

    def on_loop_mode_radio_changed(self):
        if self.loop_mode_var.get() == "COUNT":
            self.ent_loop_max_count.config(state="normal")
        else:
            self.ent_loop_max_count.config(state="disabled")

    def sync_target_to_loop_tab(self, scope_type, sector_num, page_num, hex_addr, length_bytes):
        """ Global Target Context Synchronizer """
        self.ent_loop_sector.delete(0, tk.END)
        self.ent_loop_sector.insert(0, str(sector_num))

        self.ent_loop_page.delete(0, tk.END)
        self.ent_loop_page.insert(0, str(page_num))

        self.ent_loop_addr.delete(0, tk.END)
        self.ent_loop_addr.insert(0, f"0x{hex_addr:06X}")

        self.ent_loop_len.delete(0, tk.END)
        self.ent_loop_len.insert(0, str(length_bytes))

        if scope_type == "PAGE":
            self.loop_scope_cb.current(0)
        elif scope_type == "SECTOR":
            self.loop_scope_cb.current(1)
        else:
            self.loop_scope_cb.current(2)

        self.update_loop_target_summary_label()

    def update_loop_target_summary_label(self):
        scope_str = self.loop_scope_cb.get()
        sec_num = self.ent_loop_sector.get().strip()
        page_num = self.ent_loop_page.get().strip()
        addr_str = self.ent_loop_addr.get().strip()
        len_str = self.ent_loop_len.get().strip()

        try:
            addr_val = int(addr_str, 16)
            len_val = int(len_str)
            end_addr = addr_val + len_val - 1
            range_str = f"0x{addr_val:06X} - 0x{end_addr:06X}"
        except ValueError:
            range_str = addr_str

        self.lbl_target_summary.config(
            text=f"🎯 TARGET: [{scope_str}] | Sector {sec_num} | Page {page_num} | Addr Range: {range_str} ({len_str} Bytes)"
        )

    def on_loop_scope_changed(self, event=None):
        scope = self.loop_scope_cb.get()
        if "Page" in scope:
            self.ent_loop_len.delete(0, tk.END)
            self.ent_loop_len.insert(0, "256")
        elif "Sector" in scope:
            self.ent_loop_len.delete(0, tk.END)
            self.ent_loop_len.insert(0, "4096")
        elif "Chip" in scope:
            self.ent_loop_len.delete(0, tk.END)
            self.ent_loop_len.insert(0, "2097152")
            self.ent_loop_addr.delete(0, tk.END)
            self.ent_loop_addr.insert(0, "0x000000")
        self.update_loop_target_summary_label()

    def on_loop_sector_input(self, event=None):
        val = self.ent_loop_sector.get().strip()
        if val.isdigit():
            sec_num = int(val)
            if 0 <= sec_num <= 511:
                addr = sec_num * 4096
                page_num = sec_num * 16
                self.ent_loop_addr.delete(0, tk.END)
                self.ent_loop_addr.insert(0, f"0x{addr:06X}")
                self.ent_loop_page.delete(0, tk.END)
                self.ent_loop_page.insert(0, str(page_num))
                self.update_loop_target_summary_label()

    def on_loop_page_input(self, event=None):
        val = self.ent_loop_page.get().strip()
        if val.isdigit():
            page_num = int(val)
            if 0 <= page_num <= 8191:
                addr = page_num * 256
                sec_num = page_num // 16
                self.ent_loop_addr.delete(0, tk.END)
                self.ent_loop_addr.insert(0, f"0x{addr:06X}")
                self.ent_loop_sector.delete(0, tk.END)
                self.ent_loop_sector.insert(0, str(sec_num))
                self.update_loop_target_summary_label()

    def on_loop_addr_input(self, event=None):
        val = self.ent_loop_addr.get().strip()
        try:
            addr = int(val, 16)
            if 0 <= addr < 0x200000:
                page_num = addr // 256
                sec_num = addr // 4096
                self.ent_loop_page.delete(0, tk.END)
                self.ent_loop_page.insert(0, str(page_num))
                self.ent_loop_sector.delete(0, tk.END)
                self.ent_loop_sector.insert(0, str(sec_num))
                self.update_loop_target_summary_label()
        except ValueError:
            pass

    def cmd_send_ops_target_to_loop(self):
        sec_num = self.ent_sector_ops.get().strip()
        page_num = self.ent_page.get().strip()
        addr_str = self.ent_addr.get().strip()
        len_str = self.ent_len.get().strip()
        try:
            sec_val = int(sec_num) if sec_num.isdigit() else 0
            page_val = int(page_num) if page_num.isdigit() else 0
            addr_val = int(addr_str, 16)
            len_val = int(len_str)
            scope_type = "SECTOR" if len_val >= 4096 else "PAGE"
            self.sync_target_to_loop_tab(scope_type, sec_val, page_val, addr_val, len_val)
            self.notebook.select(4)
        except ValueError:
            messagebox.showerror("Error", "Invalid parameter inputs in Operations tab!")

    def loop_log(self, text):
        self.txt_loop_log.config(state="normal")
        self.txt_loop_log.insert(tk.END, text + "\n")
        self.txt_loop_log.see(tk.END)
        self.txt_loop_log.config(state="disabled")

    def cmd_clear_loop_log(self):
        self.txt_loop_log.config(state="normal")
        self.txt_loop_log.delete('1.0', tk.END)
        self.txt_loop_log.config(state="disabled")

    def reset_loop_stats(self):
        self.loop_pass_cnt = 0
        self.loop_fail_cnt = 0
        self.loop_current_iter = 0
        self.lbl_stat_iter.config(text="0 / ∞")
        self.lbl_stat_pass.config(text="0")
        self.lbl_stat_fail.config(text="0")

    def cmd_start_target_loop(self):
        if not self.check_jedec_requirement(): return
        if not self.ser or not self.ser.is_open:
            messagebox.showwarning("Warning", "Please connect to COM Port first!")
            return

        scope = self.loop_scope_cb.get()
        op_mode = self.loop_op_cb.get()
        pat_idx = int(self.loop_pat_cb.get().split(":")[0])
        custom_hex = self.ent_loop_custom_hex.get().strip()
        addr_str = self.ent_loop_addr.get().strip()
        len_str = self.ent_loop_len.get().strip()
        delay_ms = int(self.ent_loop_delay.get().strip()) if self.ent_loop_delay.get().strip().isdigit() else 50
        
        is_fixed_count = (self.loop_mode_var.get() == "COUNT")
        max_count = int(self.ent_loop_max_count.get().strip()) if is_fixed_count and self.ent_loop_max_count.get().strip().isdigit() else 1000
        stop_on_fail = self.stop_on_fail_var.get()

        self.stop_requested = False
        self.loop_running = True
        self.reset_loop_stats()

        self.loop_log("==========================================")
        self.loop_log(f"🚀 STARTING TARGETED STRESS LOOP ENGINE")
        self.loop_log(f"   Target: {scope} | Addr: {addr_str} | Len: {len_str} Bytes")
        self.loop_log(f"   Op Mode: {op_mode} | Pattern #{pat_idx} (0x{custom_hex})")
        self.loop_log("==========================================")

        while self.loop_running and not self.stop_requested:
            self.loop_current_iter += 1
            max_str = str(max_count) if is_fixed_count else "∞"
            self.lbl_stat_iter.config(text=f"{self.loop_current_iter} / {max_str}")
            self.update_phase_status(f"[ Loop #{self.loop_current_iter}/{max_str} ]")

            is_pass = False

            if "Verify Pattern" in op_mode:
                if "Entire Chip" in scope:
                    resp = self.execute_cmd_with_progress(f"VERIFY_FULL_PAT:{pat_idx}:{custom_hex}", ["VERIFY:OK", "VERIFY:FAIL:"])
                else:
                    resp = self.execute_cmd_with_progress(f"VERIFY:{addr_str}:{len_str}:{pat_idx}:{custom_hex}", ["VERIFY:OK", "VERIFY:FAIL:"])
                if resp == "VERIFY:OK":
                    is_pass = True
                elif resp and resp.startswith("VERIFY:FAIL:"):
                    parts = resp.split(":")
                    self.loop_log(f"❌ Loop #{self.loop_current_iter}: FAIL! ({parts[2]} errors). Mismatch @ {parts[3]}")

            elif "Write Pattern" in op_mode:
                if "Entire Chip" in scope:
                    resp = self.execute_cmd_with_progress(f"WRITE_FULL_PAT:{pat_idx}:{custom_hex}", ["WRITE_PAT:OK"])
                else:
                    resp = self.execute_cmd_with_progress(f"WRITE_PAT:{addr_str}:{len_str}:{pat_idx}:{custom_hex}", ["WRITE_PAT:OK"])
                if resp == "WRITE_PAT:OK":
                    is_pass = True

            elif "Erase -> Write -> Verify" in op_mode:
                # Erase -> Write -> Verify Cycle
                sec_val = self.ent_loop_sector.get().strip()
                sec_addr = f"0x{int(sec_val)*4096:06X}" if sec_val.isdigit() else addr_str
                resp_erase = self.execute_cmd_with_progress(f"ERASE:{sec_addr}", ["ERASE:OK"])
                if resp_erase == "ERASE:OK":
                    resp_write = self.execute_cmd_with_progress(f"WRITE_PAT:{addr_str}:{len_str}:{pat_idx}:{custom_hex}", ["WRITE_PAT:OK"])
                    if resp_write == "WRITE_PAT:OK":
                        resp_ver = self.execute_cmd_with_progress(f"VERIFY:{addr_str}:{len_str}:{pat_idx}:{custom_hex}", ["VERIFY:OK", "VERIFY:FAIL:"])
                        if resp_ver == "VERIFY:OK":
                            is_pass = True
                        elif resp_ver and resp_ver.startswith("VERIFY:FAIL:"):
                            parts = resp_ver.split(":")
                            self.loop_log(f"❌ Loop #{self.loop_current_iter}: ERASE-WRITE-VERIFY FAIL! Mismatch @ {parts[3]}")

            elif "Read Test" in op_mode:
                resp = self.execute_cmd_with_progress(f"READ:{addr_str}:{len_str}", ["DATA:"])
                if resp and resp.startswith("DATA:"):
                    is_pass = True

            if is_pass:
                self.loop_pass_cnt += 1
                self.lbl_stat_pass.config(text=str(self.loop_pass_cnt))
                if self.loop_current_iter % 10 == 0 or self.loop_current_iter == 1:
                    self.loop_log(f"🟢 Loop #{self.loop_current_iter}: PASSED")
            else:
                self.loop_fail_cnt += 1
                self.lbl_stat_fail.config(text=str(self.loop_fail_cnt))
                if stop_on_fail:
                    self.loop_log(f"🛑 [STOPPED] Loop terminated immediately on first failure @ Loop #{self.loop_current_iter}")
                    self.loop_running = False
                    break

            if is_fixed_count and self.loop_current_iter >= max_count:
                self.loop_log(f"✅ Target Loop count of {max_count} iterations completed!")
                self.loop_running = False
                break

            if delay_ms > 0 and self.loop_running and not self.stop_requested:
                time.sleep(delay_ms / 1000.0)

        self.loop_running = False
        self.update_progress(0)
        self.update_phase_status("[ State: IDLE ]")

    # -------------------------------------------------------------------------
    # System Operations & Handlers
    # -------------------------------------------------------------------------
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
                    parts = line.split(":")
                    addr_hex = parts[1]
                    sample_detail = parts[2] if len(parts) >= 3 else "Mismatch"
                    try:
                        addr_val = int(addr_hex, 16)
                        page_addr = addr_val & ~0xFF
                        sec_val = addr_val // 4096
                        self.tested_sectors.add(sec_val)

                        if page_addr not in self.failed_pages:
                            self.failed_pages.append(page_addr)
                            self.failed_page_err_counts[page_addr] = 1
                            self.failed_page_samples[page_addr] = f"@{addr_hex} {sample_detail}"
                        else:
                            self.failed_page_err_counts[page_addr] += 1
                    except ValueError:
                        pass

                    if printed_errors <= 20:
                        self.ops_log(f"  ❌ Mismatch @ {parts[1]} | Details: {sample_detail}")
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
        if not self.check_jedec_requirement(): return
        if not self.ser or not self.ser.is_open:
            messagebox.showwarning("Warning", "Please connect to COM Port first!")
            return

        is_loop = 1 if self.scope_cb.get() == "Continuous Loop" else 0
        self.stop_requested = False
        
        self.failed_pages.clear()
        self.failed_page_err_counts.clear()
        self.failed_page_samples.clear()
        self.tested_sectors.clear()
        self.current_fail_idx = -1

        self.ops_log(f"\n==========================================")
        self.ops_log(f"🚀 STARTING ALL-PATTERN FULL-CHIP SWEEP")
        self.ops_log(f"==========================================")
        
        self.ser.reset_input_buffer()
        self.ser.write(f"ALL_PAT_START:{is_loop}\n".encode('utf-8'))

        pat_names = ["0xFF", "0x00", "0xAA", "0x55", "Custom", "Pseudo-Random", "Alt 0xAA/0x55", "Inc (0x00-0xFF)"]
        curr_cycle = 1
        curr_pat = 0
        curr_phase = "START"
        printed_errors = 0

        old_timeout = self.ser.timeout
        self.ser.timeout = 0.1

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
                self.last_verified_pat_type = curr_pat
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
                addr_hex = parts[1]
                sample_detail = parts[2] if len(parts) >= 3 else "Mismatch"
                try:
                    addr_val = int(addr_hex, 16)
                    page_addr = addr_val & ~0xFF
                    sec_val = addr_val // 4096
                    self.tested_sectors.add(sec_val)

                    if page_addr not in self.failed_pages:
                        self.failed_pages.append(page_addr)
                        self.failed_page_err_counts[page_addr] = 1
                        self.failed_page_samples[page_addr] = f"@{addr_hex} {sample_detail}"
                    else:
                        self.failed_page_err_counts[page_addr] += 1
                except ValueError:
                    pass

                if printed_errors <= 20:
                    self.ops_log(f"    ❌ Mismatch @ {parts[1]} | {sample_detail}")

            elif line.startswith("PAT_DONE:"):
                parts = line.split(":")
                pat_id, errs = int(parts[1]), int(parts[2])
                p_name = pat_names[pat_id] if pat_id < len(pat_names) else f"#{pat_id}"
                if errs == 0:
                    self.ops_log(f"  🟢 Pat #{pat_id} ({p_name}) PASSED!")
                    self.tested_sectors.update(range(512))
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

        self.update_fa_table_data()
        self.render_bitmap_grid()
        self.update_fail_navigation_ui()
        if self.failed_pages:
            self.notebook.select(1)

    def cmd_standalone_erase_chip(self):
        if not self.check_jedec_requirement(): return
        if messagebox.askyesno("Confirm Chip Erase", "Are you sure you want to erase the ENTIRE CHIP (2MB)?"):
            self.update_phase_status("[ Phase: ERASING CHIP... ]")
            self.ops_log("[ACTION] Erasing ENTIRE CHIP (2 Megabytes)... Please wait...")
            resp = self.execute_cmd_with_progress("ERASE_CHIP", ["ERASE_CHIP:OK"])
            if resp == "ERASE_CHIP:OK":
                self.ops_log("[SUCCESS] 💥 Entire Chip Erased Successfully!")
            self.update_phase_status("[ State: IDLE ]")

    def cmd_execute_action(self):
        if not self.check_jedec_requirement(): return

        mode = self.op_mode_cb.get()
        scope = self.scope_cb.get()

        if "Sweep" in mode:
            self.run_all_pattern_sweep_engine()
            return

        addr = self.ent_addr.get().strip()
        length = self.ent_len.get().strip()
        pat_idx = int(self.pat_cb.get().split(":")[0])
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
            self.failed_pages.clear()
            self.failed_page_err_counts.clear()
            self.failed_page_samples.clear()
            self.tested_sectors.clear()
            self.current_fail_idx = -1
            self.last_verified_pat_type = pat_idx
            self.last_verified_custom_hex = custom_hex

            if scope == "Entire Chip (2MB)":
                self.ops_log(f"[ACTION] Verifying ENTIRE CHIP with Pattern #{pat_idx}...")
                t0 = time.time()
                resp = self.execute_cmd_with_progress(f"VERIFY_FULL_PAT:{pat_idx}:{custom_hex}", ["VERIFY:OK", "VERIFY:FAIL:"])
                t1 = time.time()
                if resp == "VERIFY:OK":
                    self.ops_log(f"[PASSED] 🟢 Full Chip Verification PASSED in {t1-t0:.2f}s!")
                    self.tested_sectors.update(range(512))
                    self.update_pin_health({2: "#34C759", 5: "#34C759"})
                elif resp and resp.startswith("VERIFY:FAIL:"):
                    parts = resp.split(":")
                    self.ops_log(f"[FAILED] 🔴 Full Chip Verification FAILED! ({parts[2]} errors). First Mismatch @ {parts[3]}")
                    self.update_pin_health({2: "#FF3B30", 5: "#FF3B30"})
            else:
                self.ops_log(f"[ACTION] Verifying Pattern #{pat_idx} at {addr} (Len: {length} bytes)...")
                t0 = time.time()
                resp = self.execute_cmd_with_progress(f"VERIFY:{addr}:{length}:{pat_idx}:{custom_hex}", ["VERIFY:OK", "VERIFY:FAIL:"])
                t1 = time.time()
                try:
                    start_addr_val = int(addr, 16)
                    len_val = int(length)
                    sec_start = start_addr_val // 4096
                    sec_end = (start_addr_val + len_val - 1) // 4096
                    for s in range(sec_start, sec_end + 1):
                        self.tested_sectors.add(s)
                except ValueError:
                    pass

                if resp == "VERIFY:OK":
                    self.ops_log(f"[PASSED] 🟢 Verification PASSED (0 Errors) in {t1-t0:.3f}s!")
                    self.update_pin_health({2: "#34C759", 5: "#34C759"})
                elif resp and resp.startswith("VERIFY:FAIL:"):
                    parts = resp.split(":")
                    self.ops_log(f"[FAILED] 🔴 Verification FAILED! ({parts[2]} errors). First Mismatch @ {parts[3]}")
                    self.update_pin_health({2: "#FF3B30", 5: "#FF3B30"})

            self.update_phase_status("[ State: IDLE ]")

            self.update_fa_table_data()
            self.render_bitmap_grid()
            self.update_fail_navigation_ui()
            if self.failed_pages:
                self.notebook.select(1)

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
            dev_name = JEDEC_DB.get(id_str, "Unknown / Unsupported Flash IC")
            self.lbl_jedec.config(text=f"JEDEC ID: {id_str}")
            self.lbl_device.config(text=f"Device: {dev_name}")
            if id_str in ["00 00 00", "FF FF FF"]:
                self.update_pin_health({1: "#FF3B30", 2: "#FF3B30", 4: "#FF3B30", 5: "#FF3B30", 6: "#FF3B30", 8: "#FF3B30"})
                self.jedec_read_valid = False
                messagebox.showwarning("Warning", f"อ่านได้ค่า JEDEC ID ({id_str}) ซึ่งไม่ถูกต้อง กรุณาเช็กการเชื่อมต่อสาย IC!")
            else:
                self.update_pin_health({1: "#34C759", 2: "#34C759", 4: "#34C759", 5: "#34C759", 6: "#34C759", 7: "#34C759", 8: "#34C759"})
                self.jedec_read_valid = True
                self.ops_log(f"[SYSTEM] ✅ Check JEDEC ID Validated: {id_str} ({dev_name})")
        else:
            self.jedec_read_valid = False
            messagebox.showerror("Error", "Failed to read JEDEC ID")
            self.update_pin_health({1: "#FF3B30", 2: "#FF3B30", 4: "#FF3B30", 5: "#FF3B30", 6: "#FF3B30", 8: "#FF3B30"})

    def cmd_unlock(self):
        if not self.check_jedec_requirement(): return
        resp = self.execute_cmd_with_progress("UNLOCK", ["UNLOCK:OK"])
        if resp == "UNLOCK:OK":
            messagebox.showinfo("Success", "Global Block Protection Unlocked!")
            self.update_lock_ui(True)
            self.cmd_read_regs()
        else:
            messagebox.showerror("Error", "Unlock failed")
            self.update_lock_ui(False)

    def cmd_read_regs(self):
        if not self.check_jedec_requirement(): return
        resp = self.execute_cmd_with_progress("READ_REGS", ["REGS:"])
        if resp and resp.startswith("REGS:"):
            parts = resp.split(":")
            stat_hex, cfg_hex = parts[1], parts[2]
            self.lbl_stat_reg.config(text=f"SR: 0x{stat_hex}")
            self.lbl_cfg_reg.config(text=f"CR: 0x{cfg_hex}")
        else:
            messagebox.showerror("Error", "Failed to read registers")

if __name__ == "__main__":
    root = Tk = tk.Tk()
    app = FlashProgrammerGUI(root)
    root.mainloop()