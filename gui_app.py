import time
import csv
import bisect
import os
import hashlib
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
import serial
import serial.tools.list_ports

# รองรับเฉพาะ SST26VF (firmware ใช้คำสั่ง Global Unlock 0x98 ซึ่งเป็นของ SST26 เท่านั้น)
# ตรวจรหัส capacity กับ datasheet อีกครั้ง (BF 26 40 ยึดตามที่ firmware ระบุ)
JEDEC_DB = {
    "BF 26 40": "Microchip SST26VF080B (1MB / 8Mbit)",
    "BF 26 41": "Microchip SST26VF016B (2MB / 16Mbit)",
    "BF 26 42": "Microchip SST26VF032B (4MB / 32Mbit)",
    "BF 26 43": "Microchip SST26VF064B (8MB / 64Mbit)",
}
CHIP_BY_CAP = {0x40: 0x100000, 0x41: 0x200000, 0x42: 0x400000, 0x43: 0x800000}
DEFAULT_CHIP_SIZE = 0x200000
SWEEP_CUSTOM_HEX = "A5"   # ต้องตรงกับ custom_vals[4] ใน firmware (runAllPatternSweep)

IO_READ_CHUNK = 4096     # ขนาดต่อคำสั่ง READ ตอน Dump/Compare (เท่ากับที่ Hex Viewer ใช้อยู่)


class FlashProgrammerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("SST26VF Tester & Failure Analysis (FA) Programmer - ESP32")
        self.root.geometry("1100x800")
        self.root.resizable(False, False)

        self.ser = None
        self.stop_requested = False
        self.jedec_read_valid = False  # Track JEDEC ID requirement
        self.is_unlocked = False       # Track Protection status
        self.ui_ready = False          # กัน event ตอนสร้าง UI ยังไม่เสร็จ
        self.busy = False              # กันสั่งงานซ้อนระหว่างที่คำสั่งกำลังรัน
        self.closing = False
        self.job_running = False       # งาน Data I/O ที่ Python คุมเอง (หลายคำสั่งต่อเนื่อง)
        self.fa_unsaved = False        # มีผล FA ที่ยังไม่ได้ export หรือไม่

        # ขนาดชิปปัจจุบัน (ปรับตาม JEDEC ID ตอน Read ID)
        self.chip_size = DEFAULT_CHIP_SIZE

        # Global Central Active Target
        self.target_hex_addr = 0x000000
        self.target_len = 4096
        self.bytes_per_hex_view = 256

        # Multi-page Failure Analysis Data Store
        self.failed_pages = []            # Unique page start addresses (เรียงตามลำดับที่พบ)
        self.failed_page_err_counts = {}  # Page Addr -> Error Count (ใช้เป็น set lookup O(1) ด้วย)
        self.failed_page_samples = {}     # Page Addr -> First Mismatch detail
        self.tested_sectors = set()       # Set of tested sector indices
        self.selected_bitmap_sector = 0

        # เก็บทั้ง pattern และ "ช่วง address" ที่ verify จริง ใช้ไฮไลต์ใน Hex Viewer
        self.last_verified_pat_type = None
        self.last_verified_custom_hex = "AA"
        self.last_verified_range = None   # (start_addr, end_addr_exclusive)

        # Loop Test State Storage
        self.loop_running = False
        self.loop_pass_cnt = 0
        self.loop_fail_cnt = 0
        self.loop_current_iter = 0

        self.bitmap_rects = {}  # sector_idx -> canvas rect ID

        style = ttk.Style()
        style.theme_use('clam')

        # =========================================================================
        # TOP HEADER: SYSTEM CONNECTION & HARDWARE DIAGNOSTICS
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

        btn_reset_ui = ttk.Button(row0, text="🔄 Reset UI", command=self.cmd_reset_ui_app)
        btn_reset_ui.pack(side="left", padx=(6, 2))

        btn_new_chip = ttk.Button(row0, text="🆕 New Chip", command=self.cmd_new_chip)
        btn_new_chip.pack(side="left", padx=2)

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

        self.lbl_lock_status = ttk.Label(row1, text="🔒 Protection: LOCKED", font=("Segoe UI", 9, "bold"), foreground="#FF3B30")
        self.lbl_lock_status.pack(side="left", padx=(12, 6))

        self.lbl_stat_reg = ttk.Label(row1, text="SR(0x05): --", font=("Consolas", 9))
        self.lbl_stat_reg.pack(side="left", padx=6)

        self.lbl_cfg_reg = ttk.Label(row1, text="CR(0x35): --", font=("Consolas", 9))
        self.lbl_cfg_reg.pack(side="left", padx=6)

        self.lbl_phase = ttk.Label(row1, text="[ State: IDLE ]", font=("Consolas", 9, "bold"), foreground="#007ACC")
        self.lbl_phase.pack(side="right", padx=5)

        # =========================================================================
        # TAB SYSTEM ARCHITECTURE
        # =========================================================================
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=5)
        self.notebook.bind("<<NotebookTabChanged>>", self.on_tab_changed)

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
        self.notebook.add(self.tab_loop, text=" 🔁 Loop ")
        self.setup_tab_loop()

        self.tab_io = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_io, text=" 💾 Data I/O ")
        self.setup_tab_io()

        self.refresh_ports()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.ui_ready = True

    # =========================================================================
    # CHIP SIZE HELPERS
    # =========================================================================
    @property
    def n_sectors(self):
        return self.chip_size // 4096

    @property
    def n_pages(self):
        return self.chip_size // 256

    @property
    def chip_label(self):
        return f"{self.chip_size // 0x100000}MB"

    def apply_chip_size(self, size):
        """ ปรับทุกอย่างที่ผูกกับขนาดชิป (เรียกหลัง Read ID สำเร็จ) """
        if size == self.chip_size:
            return
        self.chip_size = size
        full = f"Entire Chip ({self.chip_label})"
        self.scope_cb['values'] = ["Selected Range", full]
        if "Entire" in self.scope_cb.get():
            self.scope_cb.set(full)
        self.btn_io_full.config(text=f"Full Chip ({self.chip_label})")
        self.selected_bitmap_sector = 0
        self.lbl_bitmap_selected.config(text="Selected: Sector 0 (0x000000 - 0x000FFF)")
        self.clear_fa_data()
        self.update_fa_table_data()
        self.render_bitmap_grid()
        self.set_active_target(0, 4096)
        self.sync_target_to_ops_ui()

    # =========================================================================
    # COMMON HELPERS
    # =========================================================================
    def on_close(self):
        self.closing = True
        self.stop_requested = True
        self.loop_running = False
        if self.busy or self.job_running:
            # รอให้คำสั่งที่กำลังรันอยู่จบก่อน แล้วค่อยปิด
            self.root.after(200, self.on_close)
            return
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception:
            pass
        self.root.destroy()

    def is_connected(self):
        return bool(self.ser and self.ser.is_open)

    def ensure_idle(self):
        """ กันสั่งงานซ้อน (root.update() ทำให้กดปุ่มอื่นได้ระหว่างรัน) """
        if self.busy or self.loop_running or self.job_running:
            messagebox.showwarning("Busy", "มีงานกำลังทำงานอยู่ กรุณารอให้เสร็จหรือกด STOP ก่อนครับ")
            return False
        return True

    def check_unlocked(self):
        if self.is_unlocked:
            return True
        return messagebox.askyesno(
            "🔒 Protection is LOCKED",
            "ยังไม่ได้กด 'Unlock Protection' การเขียน/ลบอาจไม่สำเร็จ\nต้องการดำเนินการต่อหรือไม่?",
            default="no")

    def validate_target(self):
        if self.target_len <= 0 or self.target_hex_addr + self.target_len > self.chip_size:
            messagebox.showerror("Error", f"Address + Length เกินขนาด chip ({self.chip_label}) หรือ Length เป็น 0")
            return False
        return True

    def read_target_entries(self, addr_entry, len_entry=None):
        """ อ่าน + validate address/length จากช่องกรอกก่อนตั้ง target (ไม่ clamp เงียบๆ)
            len_entry=None -> ตรวจเฉพาะ address (เช่น Erase Sector) """
        try:
            addr = int(addr_entry.get().strip(), 16)
        except ValueError:
            messagebox.showerror("Error", "Invalid Hex Address format")
            return False
        if addr < 0 or addr >= self.chip_size:
            messagebox.showerror("Error", f"Address 0x{addr:X} เกินขนาด chip (0x000000 - 0x{self.chip_size - 1:06X})")
            return False

        if len_entry is not None:
            ln = len_entry.get().strip()
            if not ln.isdigit():
                messagebox.showerror("Error", "Length ต้องเป็นตัวเลขจำนวนเต็ม")
                return False
            self.target_len = int(ln)
            self.target_hex_addr = addr
            self.update_target_info_labels()
            return self.validate_target()

        self.target_hex_addr = addr
        self.update_target_info_labels()
        return True

    @staticmethod
    def normalize_hex_byte(s, default="AA"):
        try:
            return f"{int(str(s).strip(), 16) & 0xFF:02X}"
        except ValueError:
            return default

    @staticmethod
    def parse_verify_fail(resp):
        parts = resp.split(":")
        cnt = parts[2] if len(parts) > 2 else "?"
        where = parts[3] if len(parts) > 3 else "?"
        return cnt, where

    def set_verified(self, pat_type, custom_hex, addr, length):
        self.last_verified_pat_type = pat_type
        self.last_verified_custom_hex = custom_hex
        self.last_verified_range = (addr, addr + length)

    def clear_verified(self):
        self.last_verified_pat_type = None
        self.last_verified_range = None

    def mark_tested(self, addr, length):
        if length <= 0:
            return
        for s in range(addr // 4096, (addr + length - 1) // 4096 + 1):
            self.tested_sectors.add(s)

    def update_lock_ui(self, is_unlocked):
        self.is_unlocked = is_unlocked
        if is_unlocked:
            self.lbl_lock_status.config(text="🔓 Protection: UNLOCKED", foreground="#34C759")
        else:
            self.lbl_lock_status.config(text="🔒 Protection: LOCKED", foreground="#FF3B30")

    def check_jedec_requirement(self):
        if not self.ensure_idle():
            return False
        if not self.jedec_read_valid:
            messagebox.showwarning("⚠️ Warning: Must Read JEDEC ID First",
                                   "กรุณากดปุ่ม 'Read JEDEC ID' เพื่อตรวจสอบ IC ก่อนเริ่มทำรายการครับ!")
            return False
        return True

    def cmd_reset_ui_app(self):
        self.stop_requested = False
        self.jedec_read_valid = False
        self.loop_running = False
        self.update_lock_ui(False)
        self.lbl_jedec.config(text="JEDEC ID: -- -- --")
        self.lbl_device.config(text="Device: -- (กด Read JEDEC ID ใหม่)", foreground="#FF9500")
        self.lbl_stat_reg.config(text="SR(0x05): --")
        self.lbl_cfg_reg.config(text="CR(0x35): --")
        if self.ser and self.ser.is_open:
            try:
                self.ser.reset_input_buffer()
                self.ser.reset_output_buffer()
            except Exception:
                pass
        self.update_progress(0)
        self.update_phase_status("[ State: IDLE ]")
        self.ops_log("[SYSTEM] 🔄 UI App & Serial Buffer refreshed and reset to IDLE.")

    def cmd_new_chip(self):
        """ 🆕 ล้างสถานะทั้งหมดที่ผูกกับชิปตัวเดิม และบังคับให้ยืนยัน JEDEC ID ใหม่ """
        if not self.ensure_idle():
            return

        if self.failed_pages and self.fa_unsaved:
            ans = messagebox.askyesnocancel(
                "🆕 New Chip",
                "มีผล Failure Analysis ของชิปตัวเดิมที่ยังไม่ได้ Export\n"
                "ต้องการ Export ก่อนหรือไม่?\n\n"
                "Yes = Export ก่อน  |  No = ทิ้งผล  |  Cancel = ยกเลิก")
            if ans is None:
                return
            if ans:
                self.cmd_export_fa_report()
                if self.fa_unsaved:  # ผู้ใช้ยกเลิกหน้าต่างบันทึก หรือบันทึกไม่สำเร็จ
                    return

        # --- ล้างข้อมูลผลทดสอบ ---
        self.clear_fa_data()
        self.update_fa_table_data()
        self.lbl_fa_status.config(text="STATUS: UNTESTED / NO ERRORS RECORDED", foreground="#007ACC")
        self.clear_verified()
        self.last_verified_custom_hex = "AA"

        self.selected_bitmap_sector = 0
        self.lbl_bitmap_selected.config(text="Selected: Sector 0 (0x000000 - 0x000FFF)")
        self.render_bitmap_grid()

        self.reset_loop_stats()
        self.loop_log("------------------------------------------")
        self.loop_log("🆕 NEW CHIP — statistics reset")
        self.loop_log("------------------------------------------")
        self.ops_log("\n------------------------------------------")
        self.ops_log("🆕 [NEW CHIP] ล้างสถานะของชิปตัวเดิมแล้ว — กรุณากด 'Read JEDEC ID' เพื่อยืนยันชิปตัวใหม่")
        self.ops_log("------------------------------------------")

        self.txt_hex_display.config(state="normal")
        self.txt_hex_display.delete('1.0', tk.END)
        self.txt_hex_display.config(state="disabled")

        # --- รีเซ็ต target กลับค่าเริ่มต้น แล้ว sync ทุกแท็บ ---
        self.target_hex_addr = 0x000000
        self.target_len = 4096
        self.sync_target_to_ops_ui()
        self.sync_target_to_loop_ui()
        self.ent_hex_jump.delete(0, tk.END)
        self.ent_hex_jump.insert(0, "0x000000")
        self.update_target_info_labels()

        # --- บังคับให้ Read ID + Unlock ใหม่ ---
        self.jedec_read_valid = False
        self.update_lock_ui(False)
        self.lbl_jedec.config(text="JEDEC ID: -- -- --")
        self.lbl_device.config(text="Device: -- (รอยืนยันชิปใหม่: กด Read JEDEC ID)", foreground="#FF9500")
        self.lbl_stat_reg.config(text="SR(0x05): --")
        self.lbl_cfg_reg.config(text="CR(0x35): --")

        if self.is_connected():
            try:
                self.ser.reset_input_buffer()
            except Exception:
                pass
        self.stop_requested = False
        self.update_progress(0)
        self.update_phase_status("[ State: IDLE ]")

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

    def drain_after_stop(self, wait=1.0):
        """ รอ ABORTED จาก ESP32 แล้วเคลียร์ buffer กันไปรบกวนคำสั่งถัดไป """
        try:
            buf = bytearray()
            end = time.time() + wait
            while time.time() < end:
                line = self.read_line(self.ser, buf)
                if line == "ABORTED":
                    break
            self.ser.reset_input_buffer()
        except Exception:
            pass

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
            self.jedec_read_valid = False
            self.update_lock_ui(False)

    def handle_disconnect(self, err=None):
        """ สาย USB หลุด/พอร์ตพัง -> หยุด loop, ปิดพอร์ต, เคลียร์ self.ser """
        self.loop_running = False
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        self.ser = None
        if self.closing:
            return
        if err is not None:
            self.ops_log(f"[ERROR] Communication failure: {err}")
            self.loop_log(f"🔌 [ERROR] Communication failure: {err}")
        self.set_led_status(False)

    # -------------------------------------------------------------------------
    # SERIAL LINE READER (สะสม buffer จนเจอ '\n' ไม่ตัดบรรทัดตอน timeout)
    # -------------------------------------------------------------------------
    @staticmethod
    def read_line(ser, buf):
        while True:
            i = buf.find(b"\n")
            if i >= 0:
                line = bytes(buf[:i])
                del buf[:i + 1]
                return line.decode('utf-8', errors='ignore').strip()
            chunk = ser.read(max(1, ser.in_waiting))
            if not chunk:
                return ""  # timeout: ข้อมูลที่ยังไม่ครบบรรทัดยังเก็บอยู่ใน buf
            buf.extend(chunk)

    def set_active_target(self, addr, length=None):
        """ Global Central Target Handler (clamp เฉพาะใช้กับการนำทาง/พิมพ์ระหว่างกรอก
            การสั่งงานจริงต้องผ่าน read_target_entries ที่ validate ก่อน) """
        self.target_hex_addr = max(0, min(addr, self.chip_size - 1))
        if length is not None:
            self.target_len = length
        self.update_target_info_labels()

    def update_target_info_labels(self):
        addr = self.target_hex_addr
        sec = addr // 4096
        page = addr // 256
        info_str = f"Sector: {sec} (0-{self.n_sectors - 1})  |  Page: {page} (0-{self.n_pages - 1})"

        if hasattr(self, 'lbl_ops_sec_page'):
            self.lbl_ops_sec_page.config(text=info_str)
        if hasattr(self, 'lbl_hex_sec_page'):
            self.lbl_hex_sec_page.config(text=info_str)
            end_addr = addr + self.bytes_per_hex_view - 1
            self.lbl_hex_page_info.config(text=f"Range: 0x{addr:06X} - 0x{end_addr:06X}")
            self.update_fail_nav_label()
        if hasattr(self, 'lbl_io_sec_page'):
            self.lbl_io_sec_page.config(text=info_str)
        if hasattr(self, 'lbl_loop_sec_page'):
            self.lbl_loop_sec_page.config(text=info_str)
            end_addr = addr + self.target_len - 1
            self.lbl_target_summary.config(text=f"🎯 ACTIVE TARGET: Range 0x{addr:06X} - 0x{end_addr:06X} ({self.target_len} Bytes)")

    def on_tab_changed(self, event=None):
        if not self.ui_ready:
            return
        selected_tab = self.notebook.tab(self.notebook.select(), "text").strip()
        if "Loop" in selected_tab:
            self.sync_target_to_loop_ui()
        elif "Hex Viewer" in selected_tab:
            self.ent_hex_jump.delete(0, tk.END)
            self.ent_hex_jump.insert(0, f"0x{self.target_hex_addr:06X}")
            self.update_target_info_labels()
        elif "Memory Operations" in selected_tab:
            self.sync_target_to_ops_ui()
        elif "Data I/O" in selected_tab:
            self.sync_target_to_io_ui()

    def sync_target_to_ops_ui(self):
        self.ent_addr.config(state="normal")
        self.ent_addr.delete(0, tk.END)
        self.ent_addr.insert(0, f"0x{self.target_hex_addr:06X}")
        self.ent_len.config(state="normal")
        self.ent_len.delete(0, tk.END)
        self.ent_len.insert(0, str(self.target_len))
        self.update_param_states()  # คืนสถานะ disable ถ้าเป็นโหมด full chip
        self.update_target_info_labels()

    def goto_hex_viewer(self, addr, length):
        """ ตั้งค่า target / ขนาด view / ช่อง address ให้เรียบร้อยก่อนโหลด """
        self.set_active_target(addr, length)
        if length >= 4096:
            self.hex_view_size_cb.current(1)
            self.bytes_per_hex_view = 4096
        else:
            self.hex_view_size_cb.current(0)
            self.bytes_per_hex_view = 256
        self.ent_hex_jump.delete(0, tk.END)
        self.ent_hex_jump.insert(0, f"0x{self.target_hex_addr:06X}")
        self.notebook.select(self.tab_hex)
        self.update_target_info_labels()
        self.load_hex_view_page()

    # =========================================================================
    # TAB 1: MEMORY OPERATIONS PANEL
    # =========================================================================
    def setup_tab_ops(self):
        cfg_frame = ttk.LabelFrame(self.tab_ops, text=" ⚙️ Operation & Memory Targeting Parameters ")
        cfg_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(cfg_frame, text="Operation Mode:").grid(row=0, column=0, padx=5, pady=6, sticky="e")
        self.op_mode_cb = ttk.Combobox(cfg_frame, width=24, state="readonly")
        self.op_mode_cb['values'] = [
            "Write Pattern",
            "Verify Pattern",
            "Erase Sector (4KB)",
            "All Patterns Sweep (Full Chip)"
        ]
        self.op_mode_cb.current(0)
        self.op_mode_cb.grid(row=0, column=1, padx=5, pady=6, sticky="w")
        self.op_mode_cb.bind("<<ComboboxSelected>>", self.update_param_states)

        ttk.Label(cfg_frame, text="Scope:").grid(row=0, column=2, padx=5, pady=6, sticky="e")
        self.scope_cb = ttk.Combobox(cfg_frame, width=18, state="readonly")
        self.scope_cb['values'] = ["Selected Range", f"Entire Chip ({self.chip_label})"]
        self.scope_cb.current(0)
        self.scope_cb.grid(row=0, column=3, padx=5, pady=6, sticky="w")
        self.scope_cb.bind("<<ComboboxSelected>>", self.update_param_states)

        ttk.Label(cfg_frame, text="Address (Hex):").grid(row=1, column=0, padx=5, pady=6, sticky="e")
        self.ent_addr = ttk.Entry(cfg_frame, width=12)
        self.ent_addr.insert(0, "0x000000")
        self.ent_addr.grid(row=1, column=1, padx=5, pady=6, sticky="w")
        self.ent_addr.bind("<KeyRelease>", self.on_ops_addr_input)

        ttk.Label(cfg_frame, text="Length (Bytes):").grid(row=1, column=2, padx=5, pady=6, sticky="e")
        self.ent_len = ttk.Entry(cfg_frame, width=12)
        self.ent_len.insert(0, "4096")
        self.ent_len.grid(row=1, column=3, padx=5, pady=6, sticky="w")
        self.ent_len.bind("<KeyRelease>", self.on_ops_len_input)

        self.lbl_ops_sec_page = ttk.Label(cfg_frame, text="Sector: 0 (0-511)  |  Page: 0 (0-8191)", font=("Consolas", 9, "bold"), foreground="#007ACC")
        self.lbl_ops_sec_page.grid(row=1, column=4, padx=15, pady=6, sticky="w")

        ttk.Label(cfg_frame, text="Pattern Type:").grid(row=2, column=0, padx=5, pady=6, sticky="e")
        self.pat_cb = ttk.Combobox(cfg_frame, width=24, state="readonly")
        self.pat_cb['values'] = [
            "0: All 0xFF",
            "1: All 0x00",
            "2: All 0xAA",
            "3: All 0x55",
            "4: Custom Hex",
            "5: Pseudo-Random",
            "6: Alt 0xAA / 0x55",
            "7: Inc (0x00-0xFF)"
        ]
        self.pat_cb.current(0)
        self.pat_cb.grid(row=2, column=1, padx=5, pady=6, sticky="w")
        self.pat_cb.bind("<<ComboboxSelected>>", self.on_pattern_changed)

        ttk.Label(cfg_frame, text="Custom Hex (0x):").grid(row=2, column=2, padx=5, pady=6, sticky="e")
        self.ent_custom_hex = ttk.Entry(cfg_frame, width=12, state="disabled")
        self.ent_custom_hex.insert(0, "AA")
        self.ent_custom_hex.grid(row=2, column=3, padx=5, pady=6, sticky="w")

        act_frame = ttk.LabelFrame(self.tab_ops, text=" 🚀 Action Launchers ")
        act_frame.pack(fill="x", padx=10, pady=4)

        btn_exec = ttk.Button(act_frame, text="▶ Execute Selected Operation", command=self.cmd_execute_action)
        btn_exec.pack(side="left", padx=6, pady=6)

        btn_all_pat = ttk.Button(act_frame, text="🔥 Run All Patterns Sweep", command=self.run_all_pattern_sweep_engine)
        btn_all_pat.pack(side="left", padx=6, pady=6)

        btn_chip_erase = ttk.Button(act_frame, text="💥 Erase Full Chip", command=self.cmd_standalone_erase_chip)
        btn_chip_erase.pack(side="left", padx=6, pady=6)

        btn_clear = ttk.Button(act_frame, text="🧹 Clear Log", command=self.cmd_clear_log)
        btn_clear.pack(side="right", padx=6, pady=6)

        log_frame = ttk.LabelFrame(self.tab_ops, text=" 📜 Operation Output Log ")
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.txt_ops_log = scrolledtext.ScrolledText(log_frame, font=("Consolas", 9), state="disabled")
        self.txt_ops_log.pack(fill="both", expand=True, padx=5, pady=5)

    def on_ops_addr_input(self, event=None):
        val = self.ent_addr.get().strip()
        try:
            addr = int(val, 16)
            self.set_active_target(addr)
        except ValueError:
            pass

    def on_ops_len_input(self, event=None):
        val = self.ent_len.get().strip()
        if val.isdigit():
            self.target_len = int(val)
            self.update_target_info_labels()

    def update_param_states(self, event=None):
        mode = self.op_mode_cb.get()
        scope = self.scope_cb.get()
        is_full_chip = ("Entire Chip" in scope) or ("Sweep" in mode)

        if is_full_chip:
            self.ent_addr.config(state="disabled")
            self.ent_len.config(state="disabled")
        else:
            self.ent_addr.config(state="normal")
            self.ent_len.config(state="normal")

        if "Sweep" in mode:
            self.scope_cb.set(f"Entire Chip ({self.chip_label})")
            self.pat_cb.config(state="disabled")
        else:
            self.pat_cb.config(state="readonly")

    def on_pattern_changed(self, event=None):
        pat_idx = self.pat_cb.get().split(":")[0]
        if pat_idx == "4":
            self.ent_custom_hex.config(state="normal")
        else:
            self.ent_custom_hex.config(state="disabled")

    # =========================================================================
    # TAB 2: DEDICATED FAILURE ANALYSIS TAB
    # =========================================================================
    def setup_tab_fa(self):
        summary_frame = ttk.LabelFrame(self.tab_fa, text=" 📈 Verification Yield & Failure Overview ")
        summary_frame.pack(fill="x", padx=10, pady=5)

        sub_grid = ttk.Frame(summary_frame)
        sub_grid.pack(fill="x", padx=10, pady=5)

        self.lbl_fa_status = ttk.Label(sub_grid, text="STATUS: UNTESTED / NO ERRORS RECORDED", font=("Segoe UI", 10, "bold"), foreground="#007ACC")
        self.lbl_fa_status.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))

        ttk.Label(sub_grid, text="Failed Pages:").grid(row=1, column=0, sticky="e", padx=4)
        self.lbl_fa_failed_pages = ttk.Label(sub_grid, text=f"0 / {self.n_pages:,} Pages", font=("Consolas", 9, "bold"))
        self.lbl_fa_failed_pages.grid(row=1, column=1, sticky="w", padx=4)

        ttk.Label(sub_grid, text="Impacted Sectors:").grid(row=1, column=2, sticky="e", padx=4)
        self.lbl_fa_failed_sectors = ttk.Label(sub_grid, text=f"0 / {self.n_sectors:,} Sectors", font=("Consolas", 9, "bold"))
        self.lbl_fa_failed_sectors.grid(row=1, column=3, sticky="w", padx=4)

        ttk.Label(sub_grid, text="Total Mismatch Bytes:").grid(row=2, column=0, sticky="e", padx=4)
        self.lbl_fa_total_bytes = ttk.Label(sub_grid, text="0 Bytes", font=("Consolas", 9, "bold"))
        self.lbl_fa_total_bytes.grid(row=2, column=1, sticky="w", padx=4)

        ttk.Label(sub_grid, text="Chip Defect Rate:").grid(row=2, column=2, sticky="e", padx=4)
        self.lbl_fa_defect_rate = ttk.Label(sub_grid, text="0.00 %", font=("Segoe UI", 9, "bold"), foreground="#34C759")
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

        btn_export = ttk.Button(action_bar, text="💾 Export FA Report (.csv)", command=self.cmd_export_fa_report)
        btn_export.pack(side="right", padx=4)

    def record_fail_line(self, line):
        """ parse FAIL_ADDR แบบทนทาน + นับจำนวน mismatch จริง (ไม่ hard-code 256)
            รองรับ  FAIL_ADDR:addr:detail          (นับ +1 ต่อบรรทัด)
                    FAIL_ADDR:addr:count:detail    (ใช้ count จาก firmware) """
        parts = line.split(":", 3)
        if len(parts) < 2:
            return
        addr_hex = parts[1]
        count = 1
        detail = "Mismatch"
        if len(parts) >= 4 and parts[2].isdigit():
            count = int(parts[2])
            detail = parts[3]
        elif len(parts) >= 3:
            detail = ":".join(parts[2:])

        try:
            addr_val = int(addr_hex, 16)
        except ValueError:
            return

        page_addr = addr_val & ~0xFF
        self.tested_sectors.add(addr_val // 4096)
        self.fa_unsaved = True

        if page_addr not in self.failed_page_err_counts:
            self.failed_pages.append(page_addr)
            self.failed_page_err_counts[page_addr] = 0
            self.failed_page_samples[page_addr] = f"@{addr_hex} {detail}"
        self.failed_page_err_counts[page_addr] = min(256, self.failed_page_err_counts[page_addr] + count)

    def clear_fa_data(self):
        self.failed_pages.clear()
        self.failed_page_err_counts.clear()
        self.failed_page_samples.clear()
        self.tested_sectors.clear()
        self.fa_unsaved = False

    def update_fa_table_data(self):
        for item in self.fa_tree.get_children():
            self.fa_tree.delete(item)
        self.refresh_fail_dropdown()

        if not self.failed_pages:
            # ขึ้น PASSED เฉพาะเมื่อมีการทดสอบเสร็จสมบูรณ์จริง
            if self.tested_sectors:
                self.lbl_fa_status.config(text="STATUS: VERIFICATION PASSED (0 ERRORS DETECTED)", foreground="#34C759")
            else:
                self.lbl_fa_status.config(text="STATUS: NO COMPLETE RESULT (aborted / not run)", foreground="#FF9500")
            self.lbl_fa_failed_pages.config(text=f"0 / {self.n_pages:,} Pages")
            self.lbl_fa_failed_sectors.config(text=f"0 / {self.n_sectors:,} Sectors")
            self.lbl_fa_total_bytes.config(text="0 Bytes")
            self.lbl_fa_defect_rate.config(text="0.00 %", foreground="#34C759")
            return

        total_failed_pages = len(self.failed_pages)
        impacted_sectors_set = set(p // 4096 for p in self.failed_pages)
        total_mismatch_bytes = sum(self.failed_page_err_counts.values())
        defect_rate = (total_mismatch_bytes / float(self.chip_size)) * 100.0

        self.lbl_fa_status.config(text=f"STATUS: VERIFICATION FAILED ({total_failed_pages} FAILING PAGES DETECTED)", foreground="#FF3B30")
        self.lbl_fa_failed_pages.config(text=f"{total_failed_pages:,} / {self.n_pages:,} Pages")
        self.lbl_fa_failed_sectors.config(text=f"{len(impacted_sectors_set):,} / {self.n_sectors:,} Sectors")
        self.lbl_fa_total_bytes.config(text=f"{total_mismatch_bytes:,} Bytes")
        self.lbl_fa_defect_rate.config(text=f"{defect_rate:.4f} %", foreground="#FF3B30")

        for idx, page_addr in enumerate(self.failed_pages):
            sec_num = page_addr // 4096
            page_num = page_addr // 256
            err_cnt = self.failed_page_err_counts.get(page_addr, 256)
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
        if not self.check_jedec_requirement():
            return

        idx = int(selected[0])
        if 0 <= idx < len(self.failed_pages):
            target_addr = self.failed_pages[idx]
            self.goto_hex_viewer(target_addr, 256)

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
                    err_cnt = self.failed_page_err_counts.get(page_addr, 256)
                    sample_str = self.failed_page_samples.get(page_addr, "")
                    writer.writerow([idx + 1, sec_num, page_num, f"0x{page_addr:06X}", err_cnt, sample_str])
            self.fa_unsaved = False
            messagebox.showinfo("Success", f"FA Report exported successfully to:\n{filepath}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to export report: {e}")

    # =========================================================================
    # TAB 3: DEDICATED FLASH BITMAP TAB
    # =========================================================================
    def setup_tab_bitmap(self):
        header_frame = ttk.LabelFrame(self.tab_bitmap, text=" 🗺️ Sector Flash Memory Block Map (4KB / Sector) ")
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

        canvas_frame = ttk.LabelFrame(self.tab_bitmap, text=" Sector Memory Matrix (32 Columns) ")
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

        ttk.Label(bottom_bar, text="💡 Tip: Double-click a sector to open Hex Viewer (red = first failing page)", font=("Segoe UI", 8, "italic"), foreground="#666666").pack(side="right", padx=5)

        self.render_bitmap_grid()

    def render_bitmap_grid(self):
        self.bitmap_canvas.delete("all")
        self.bitmap_rects.clear()

        cols = 32
        rows = self.n_sectors // cols
        box_w, box_h = 28, 24
        pad_x, pad_y = 3, 4
        start_x, start_y = 10, 10

        impacted_sectors = set(p // 4096 for p in self.failed_pages)

        for sec in range(self.n_sectors):
            r = sec // cols
            c = sec % cols

            x1 = start_x + c * (box_w + pad_x)
            y1 = start_y + r * (box_h + pad_y)
            x2 = x1 + box_w
            y2 = y1 + box_h

            if sec in impacted_sectors:
                fill_color, outline_color = "#FF3B30", "#B30000"
            elif sec in self.tested_sectors:
                fill_color, outline_color = "#34C759", "#1D8339"
            else:
                fill_color, outline_color = "#333333", "#555555"

            if sec == self.selected_bitmap_sector:
                outline_color = "#FFCC00"

            rect_id = self.bitmap_canvas.create_rectangle(x1, y1, x2, y2, fill=fill_color, outline=outline_color, width=2 if sec == self.selected_bitmap_sector else 1)

            text_color = "#FFFFFF" if sec in impacted_sectors or sec in self.tested_sectors or sec == self.selected_bitmap_sector else "#888888"
            self.bitmap_canvas.create_text(x1 + box_w // 2, y1 + box_h // 2, text=str(sec), fill=text_color, font=("Consolas", 7, "bold"))

            self.bitmap_rects[sec] = rect_id

        total_w = start_x + cols * (box_w + pad_x) + 10
        total_h = start_y + rows * (box_h + pad_y) + 10
        self.bitmap_canvas.config(scrollregion=(0, 0, total_w, total_h))

    def on_bitmap_click(self, event):
        cols = 32
        rows = self.n_sectors // cols
        box_w, box_h = 28, 24
        pad_x, pad_y = 3, 4
        start_x, start_y = 10, 10

        click_x = self.bitmap_canvas.canvasx(event.x)
        click_y = self.bitmap_canvas.canvasy(event.y)

        c = int((click_x - start_x) // (box_w + pad_x))
        r = int((click_y - start_y) // (box_h + pad_y))

        if 0 <= c < cols and 0 <= r < rows:
            sec = r * cols + c
            if 0 <= sec < self.n_sectors:
                self.selected_bitmap_sector = sec
                start_addr = sec * 4096
                self.set_active_target(start_addr, 4096)

                end_addr = start_addr + 4095
                impacted_sectors = set(p // 4096 for p in self.failed_pages)

                if sec in impacted_sectors:
                    st_str = "🔴 FAILED"
                elif sec in self.tested_sectors:
                    st_str = "🟢 PASSED"
                else:
                    st_str = "⬜ UNTESTED"

                self.lbl_bitmap_selected.config(text=f"Sector {sec} (0x{start_addr:06X} - 0x{end_addr:06X}) | Status: {st_str}")
                self.render_bitmap_grid()

    def on_bitmap_double_click(self, event):
        self.on_bitmap_click(event)
        self.cmd_inspect_bitmap_selection()

    def cmd_inspect_bitmap_selection(self):
        if not self.check_jedec_requirement():
            return
        sec = self.selected_bitmap_sector
        # sector สีแดง -> ไปที่ page แรกที่เสียใน sector นั้น / sector อื่น -> page แรกของ sector
        fails = sorted(p for p in self.failed_pages if p // 4096 == sec)
        addr = fails[0] if fails else sec * 4096
        self.goto_hex_viewer(addr, 256)

    # =========================================================================
    # TAB 4: DEDICATED HEX VIEWER
    # =========================================================================
    def setup_tab_hex(self):
        nav_frame = ttk.LabelFrame(self.tab_hex, text=" Memory Navigation & Range Controls ")
        nav_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(nav_frame, text="View Size:").grid(row=0, column=0, padx=5, pady=6, sticky="e")
        self.hex_view_size_cb = ttk.Combobox(nav_frame, width=18, state="readonly")
        self.hex_view_size_cb['values'] = ["Page (256 Bytes)", "Sector (4KB / 4096B)"]
        self.hex_view_size_cb.current(0)
        self.hex_view_size_cb.grid(row=0, column=1, padx=5, pady=6)
        self.hex_view_size_cb.bind("<<ComboboxSelected>>", self.on_hex_view_size_changed)

        ttk.Label(nav_frame, text="Address (Hex):").grid(row=0, column=2, padx=5, pady=6, sticky="e")
        self.ent_hex_jump = ttk.Entry(nav_frame, width=12)
        self.ent_hex_jump.insert(0, "0x000000")
        self.ent_hex_jump.grid(row=0, column=3, padx=5, pady=6)
        self.ent_hex_jump.bind("<KeyRelease>", self.on_hex_jump_input)

        self.lbl_hex_sec_page = ttk.Label(nav_frame, text="Sector: 0 (0-511)  |  Page: 0 (0-8191)", font=("Consolas", 9, "bold"), foreground="#007ACC")
        self.lbl_hex_sec_page.grid(row=0, column=4, padx=12, pady=6, sticky="w")

        ttk.Button(nav_frame, text="◀ Prev ", command=self.cmd_hex_prev_block).grid(row=0, column=5, padx=4, pady=6)
        ttk.Button(nav_frame, text="Next ▶", command=self.cmd_hex_next_block).grid(row=0, column=6, padx=4, pady=6)

        btn_load_hex = ttk.Button(nav_frame, text="📥 Load Hex Data", command=self.load_hex_view_page)
        btn_load_hex.grid(row=0, column=7, padx=6, pady=6)

        self.lbl_hex_page_info = ttk.Label(nav_frame, text="Range: 0x000000 - 0x0000FF", font=("Consolas", 9, "bold"))
        self.lbl_hex_page_info.grid(row=0, column=8, padx=10, pady=6)

        # แถวที่ 2: Dropdown เลือก page ที่เสีย (ข้าม sector ได้)
        self._fail_dd_pages = []
        self._fail_dd_by_sector = {}
        self._fail_dd_sectors = []
        self._fail_dd_cur_sector = None
        fail_nav = ttk.Frame(nav_frame)
        fail_nav.grid(row=1, column=0, columnspan=9, sticky="w", padx=5, pady=(0, 6))
        ttk.Label(fail_nav, text="Failing Sector:").pack(side="left", padx=4)
        self.fail_sector_cb = ttk.Combobox(fail_nav, width=22, state="readonly", font=("Consolas", 9))
        self.fail_sector_cb.pack(side="left", padx=4)
        self.fail_sector_cb.bind("<<ComboboxSelected>>", self.on_fail_sector_selected)
        ttk.Label(fail_nav, text="Page:").pack(side="left", padx=(8, 2))
        self.fail_page_cb = ttk.Combobox(fail_nav, width=34, state="readonly", font=("Consolas", 9))
        self.fail_page_cb.pack(side="left", padx=4)
        self.fail_page_cb.bind("<<ComboboxSelected>>", self.on_fail_page_selected)
        self.lbl_fail_nav = ttk.Label(fail_nav, text="Failing pages: 0", font=("Consolas", 9, "bold"), foreground="#666666")
        self.lbl_fail_nav.pack(side="left", padx=10)

        view_frame = ttk.LabelFrame(self.tab_hex, text=" Hex Data Memory Array ")
        view_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.txt_hex_display = scrolledtext.ScrolledText(view_frame, font=("Consolas", 10), state="disabled", bg="#1E1E1E", fg="#D4D4D4")
        self.txt_hex_display.pack(fill="both", expand=True, padx=5, pady=5)
        self.txt_hex_display.tag_config("mismatch", background="#FF3B30", foreground="#FFFFFF")

    def on_hex_jump_input(self, event=None):
        val = self.ent_hex_jump.get().strip()
        try:
            addr = int(val, 16)
            self.set_active_target(addr)
        except ValueError:
            pass

    def on_hex_view_size_changed(self, event=None):
        if "Sector" in self.hex_view_size_cb.get():
            self.bytes_per_hex_view = 4096
        else:
            self.bytes_per_hex_view = 256
        self.update_target_info_labels()

    def cmd_hex_prev_block(self):
        if self.target_hex_addr >= self.bytes_per_hex_view:
            self.set_active_target(self.target_hex_addr - self.bytes_per_hex_view)
            self.ent_hex_jump.delete(0, tk.END)
            self.ent_hex_jump.insert(0, f"0x{self.target_hex_addr:06X}")

    def cmd_hex_next_block(self):
        if self.target_hex_addr + self.bytes_per_hex_view < self.chip_size:
            self.set_active_target(self.target_hex_addr + self.bytes_per_hex_view)
            self.ent_hex_jump.delete(0, tk.END)
            self.ent_hex_jump.insert(0, f"0x{self.target_hex_addr:06X}")

    def refresh_fail_dropdown(self):
        """ สร้างรายการ Sector/Page ที่เสียใหม่ (เรียกหลังทดสอบเสร็จ / New Chip) """
        if not hasattr(self, 'fail_page_cb'):
            return
        pages = sorted(self.failed_pages)
        by_sec = {}
        for p in pages:
            by_sec.setdefault(p // 4096, []).append(p)
        self._fail_dd_pages = pages
        self._fail_dd_by_sector = by_sec
        self._fail_dd_sectors = sorted(by_sec)
        self._fail_dd_cur_sector = None
        self.fail_sector_cb['values'] = [f"Sector {s} ({len(by_sec[s])} pg)" for s in self._fail_dd_sectors]
        self.fail_sector_cb.set("")
        self.fail_page_cb['values'] = []
        self.fail_page_cb.set("")
        self.update_fail_nav_label()

    def fill_fail_page_cb(self, sec):
        """ ใส่รายการ page ที่เสียของ sector นั้น (สูงสุด 16 รายการ) """
        if self._fail_dd_cur_sector != sec:
            self._fail_dd_cur_sector = sec
            self.fail_page_cb['values'] = [
                f"Page {p // 256} | 0x{p:06X} | {self.failed_page_err_counts.get(p, 0)} B"
                for p in self._fail_dd_by_sector.get(sec, [])
            ]

    def update_fail_nav_label(self):
        """ sync dropdown ทั้งสองช่องกับ page ที่กำลังดูอยู่ """
        if not hasattr(self, 'lbl_fail_nav'):
            return
        pages = self._fail_dd_pages
        if not pages:
            self.fail_sector_cb.set("")
            self.fail_page_cb['values'] = []
            self.fail_page_cb.set("")
            self._fail_dd_cur_sector = None
            self.lbl_fail_nav.config(text="Failing pages: 0", foreground="#666666")
            return

        cur = self.target_hex_addr & ~0xFF
        sec = cur // 4096
        sec_pages = self._fail_dd_by_sector.get(sec)
        total = len(pages)

        if sec_pages:
            self.fail_sector_cb.current(self._fail_dd_sectors.index(sec))
            self.fill_fail_page_cb(sec)
            if cur in sec_pages:
                self.fail_page_cb.current(sec_pages.index(cur))
                gi = bisect.bisect_left(pages, cur)
                self.lbl_fail_nav.config(text=f"Failing page {gi + 1} / {total}", foreground="#FF3B30")
            else:
                self.fail_page_cb.set("")
                self.lbl_fail_nav.config(text=f"Failing pages: {total}", foreground="#FF3B30")
        else:
            self.fail_sector_cb.set("")
            self.fail_page_cb['values'] = []
            self.fail_page_cb.set("")
            self._fail_dd_cur_sector = None
            self.lbl_fail_nav.config(text=f"Failing pages: {total}", foreground="#FF3B30")

    def on_fail_sector_selected(self, event=None):
        """ เลือก sector -> กระโดดไป page แรกที่เสียของ sector นั้น """
        idx = self.fail_sector_cb.current()
        if 0 <= idx < len(self._fail_dd_sectors):
            sec = self._fail_dd_sectors[idx]
            self.goto_hex_viewer(self._fail_dd_by_sector[sec][0], 256)

    def on_fail_page_selected(self, event=None):
        sec = self._fail_dd_cur_sector
        idx = self.fail_page_cb.current()
        sec_pages = self._fail_dd_by_sector.get(sec, [])
        if 0 <= idx < len(sec_pages):
            self.goto_hex_viewer(sec_pages[idx], 256)

    def get_expected_byte(self, offset, pat_type, custom_hex_str="AA"):
        if pat_type == 0:
            return 0xFF
        elif pat_type == 1:
            return 0x00
        elif pat_type == 2:
            return 0xAA
        elif pat_type == 3:
            return 0x55
        elif pat_type == 4:
            try:
                return int(custom_hex_str, 16) & 0xFF
            except ValueError:
                return 0xAA
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

    def is_mismatch_byte(self, abs_addr, b):
        """ ไฮไลต์เฉพาะ address ที่อยู่ในช่วงที่ verify จริงเท่านั้น """
        if self.last_verified_pat_type is None or not self.last_verified_range:
            return False
        lo, hi = self.last_verified_range
        if not (lo <= abs_addr < hi):
            return False
        return b != self.get_expected_byte(abs_addr, self.last_verified_pat_type, self.last_verified_custom_hex)

    def load_hex_view_page(self):
        if not self.check_jedec_requirement():
            return
        try:
            addr = int(self.ent_hex_jump.get().strip(), 16)
            addr = addr & ~0xF
            if 0 <= addr and addr + self.bytes_per_hex_view <= self.chip_size:
                self.set_active_target(addr)
            else:
                messagebox.showerror("Error", f"Address/View size out of range (0x000000 - 0x{self.chip_size - 1:06X})")
                return
        except ValueError:
            messagebox.showerror("Error", "Invalid Hex Address format")
            return

        self.update_phase_status("[ Phase: LOADING HEX DATA... ]")
        addr_str = f"0x{self.target_hex_addr:06X}"
        resp = self.execute_cmd_with_progress(f"READ:{addr_str}:{self.bytes_per_hex_view}", ["DATA:"])
        if not resp or not resp.startswith("DATA:"):
            self.update_phase_status("[ State: IDLE ]")
            return

        raw_hex_list = resp[5:].strip().split()
        try:
            raw_bytes = bytes([int(x, 16) for x in raw_hex_list])
        except ValueError:
            self.ops_log("[ERROR] Hex data parse error (invalid DATA line)")
            self.update_phase_status("[ State: IDLE ]")
            return

        if len(raw_bytes) != self.bytes_per_hex_view:
            self.ops_log(f"[WARN] Expected {self.bytes_per_hex_view} bytes but received {len(raw_bytes)}")

        self.txt_hex_display.config(state="normal")
        self.txt_hex_display.delete('1.0', tk.END)

        header = "Offset    00 01 02 03 04 05 06 07  08 09 0A 0B 0C 0D 0E 0F  Decoded ASCII\n"
        header += "-" * 77 + "\n"
        self.txt_hex_display.insert(tk.END, header)

        for i in range(0, len(raw_bytes), 16):
            chunk = raw_bytes[i:i + 16]
            row_addr = f"{self.target_hex_addr + i:08X}"

            self.txt_hex_display.insert(tk.END, f"{row_addr}  ")

            for j in range(len(chunk)):
                b = chunk[j]
                abs_addr = self.target_hex_addr + i + j
                hex_str = f"{b:02X}"
                if self.is_mismatch_byte(abs_addr, b):
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
                if self.is_mismatch_byte(abs_addr, b):
                    self.txt_hex_display.insert(tk.END, char_str, "mismatch")
                else:
                    self.txt_hex_display.insert(tk.END, char_str)

            self.txt_hex_display.insert(tk.END, "|\n")

        self.txt_hex_display.config(state="disabled")
        self.update_phase_status("[ State: IDLE ]")

    # =========================================================================
    # TAB 5: DEDICATED TARGETED LOOP & STRESS TEST TAB
    # =========================================================================
    def setup_tab_loop(self):
        target_frame = ttk.LabelFrame(self.tab_loop, text=" 🎯 Central Active Target Context ")
        target_frame.pack(fill="x", padx=10, pady=5)

        t_row = ttk.Frame(target_frame)
        t_row.pack(fill="x", padx=6, pady=4)

        ttk.Label(t_row, text="Address (Hex):").grid(row=0, column=0, padx=4, pady=2, sticky="e")
        self.ent_loop_addr = ttk.Entry(t_row, width=12)
        self.ent_loop_addr.insert(0, "0x000000")
        self.ent_loop_addr.grid(row=0, column=1, padx=4, pady=2, sticky="w")
        self.ent_loop_addr.bind("<KeyRelease>", self.on_loop_addr_input)

        ttk.Label(t_row, text="Length:").grid(row=0, column=2, padx=4, pady=2, sticky="e")
        self.ent_loop_len = ttk.Entry(t_row, width=10)
        self.ent_loop_len.insert(0, "4096")
        self.ent_loop_len.grid(row=0, column=3, padx=4, pady=2, sticky="w")
        self.ent_loop_len.bind("<KeyRelease>", self.on_loop_len_input)

        self.lbl_loop_sec_page = ttk.Label(t_row, text="Sector: 0 (0-511)  |  Page: 0 (0-8191)", font=("Consolas", 9, "bold"), foreground="#007ACC")
        self.lbl_loop_sec_page.grid(row=0, column=4, padx=12, pady=2, sticky="w")

        self.lbl_target_summary = ttk.Label(target_frame, text="🎯 ACTIVE TARGET: Range 0x000000 - 0x000FFF (4096 Bytes)", font=("Consolas", 9, "bold"), foreground="#007ACC")
        self.lbl_target_summary.pack(anchor="w", padx=10, pady=(2, 6))

        cfg_frame = ttk.LabelFrame(self.tab_loop, text=" ⚙️ Loop Execution Parameters & Stress Options ")
        cfg_frame.pack(fill="x", padx=10, pady=5)

        c_row1 = ttk.Frame(cfg_frame)
        c_row1.pack(fill="x", padx=6, pady=4)

        ttk.Label(c_row1, text="Stress Operation:").grid(row=0, column=0, padx=4, pady=2, sticky="e")
        self.loop_op_cb = ttk.Combobox(c_row1, width=22, state="readonly")
        self.loop_op_cb['values'] = [
            "🔍 Verify Pattern",
            "✍️ Write Pattern",
            "🔄 Erase -> Write -> Verify"
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
        self.loop_pat_cb.current(2)
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

        stat_frame = ttk.LabelFrame(self.tab_loop, text=" 📊 Real-time Stress Test Statistics ")
        stat_frame.pack(fill="x", padx=10, pady=5)

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

        log_frame = ttk.LabelFrame(self.tab_loop, text=" 📜 Loop Event & Error Log History ")
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.txt_loop_log = scrolledtext.ScrolledText(log_frame, font=("Consolas", 9), state="disabled", bg="#1E1E1E", fg="#D4D4D4")
        self.txt_loop_log.pack(fill="both", expand=True, padx=5, pady=5)

    def sync_target_to_loop_ui(self):
        self.ent_loop_addr.delete(0, tk.END)
        self.ent_loop_addr.insert(0, f"0x{self.target_hex_addr:06X}")
        self.ent_loop_len.delete(0, tk.END)
        self.ent_loop_len.insert(0, str(self.target_len))
        self.update_target_info_labels()

    def on_loop_addr_input(self, event=None):
        val = self.ent_loop_addr.get().strip()
        try:
            addr = int(val, 16)
            self.set_active_target(addr)
        except ValueError:
            pass

    def on_loop_len_input(self, event=None):
        val = self.ent_loop_len.get().strip()
        if val.isdigit():
            self.target_len = int(val)
            self.update_target_info_labels()

    def on_loop_mode_radio_changed(self):
        if self.loop_mode_var.get() == "COUNT":
            self.ent_loop_max_count.config(state="normal")
        else:
            self.ent_loop_max_count.config(state="disabled")

    def loop_log(self, text):
        if self.closing:  # กัน TclError หลังปิดหน้าต่าง
            return
        try:
            self.txt_loop_log.config(state="normal")
            self.txt_loop_log.insert(tk.END, text + "\n")
            self.txt_loop_log.see(tk.END)
            self.txt_loop_log.config(state="disabled")
        except tk.TclError:
            pass

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

    def loop_sleep(self, delay_ms):
        """ หน่วงแบบไม่ทำให้ UI ค้าง และกด STOP ติดระหว่างรอ """
        end = time.time() + delay_ms / 1000.0
        while time.time() < end and self.loop_running and not self.closing:
            self.root.update()
            time.sleep(0.01)

    def cmd_start_target_loop(self):
        if self.loop_running:  # กันกด START ซ้อน
            return
        if not self.check_jedec_requirement():
            return
        if not self.is_connected():
            messagebox.showwarning("Warning", "Please connect to COM Port first!")
            return

        op_mode = self.loop_op_cb.get()

        # อ่าน + validate จากช่องกรอก (ไม่ clamp เงียบๆ)
        if not self.read_target_entries(self.ent_loop_addr, self.ent_loop_len):
            return
        if ("Write" in op_mode or "Erase" in op_mode) and not self.check_unlocked():
            return

        pat_idx = int(self.loop_pat_cb.get().split(":")[0])
        custom_hex = self.normalize_hex_byte(self.ent_loop_custom_hex.get(), "AA")
        addr_str = f"0x{self.target_hex_addr:06X}"
        len_str = str(self.target_len)
        delay_ms = int(self.ent_loop_delay.get().strip()) if self.ent_loop_delay.get().strip().isdigit() else 50

        is_fixed_count = (self.loop_mode_var.get() == "COUNT")
        max_count = int(self.ent_loop_max_count.get().strip()) if is_fixed_count and self.ent_loop_max_count.get().strip().isdigit() else 1000
        stop_on_fail = self.stop_on_fail_var.get()

        t_addr = self.target_hex_addr
        t_len = self.target_len
        first_sec = t_addr // 4096
        last_sec = (t_addr + t_len - 1) // 4096
        does_verify = "Verify" in op_mode

        self.stop_requested = False
        self.loop_running = True
        self.reset_loop_stats()

        # เริ่ม loop ใหม่ -> ล้างผล FA เก่า ไม่ให้ปนกัน
        if does_verify:
            self.clear_fa_data()
            self.clear_verified()

        self.loop_log("==========================================")
        self.loop_log("🚀 STARTING TARGETED STRESS LOOP ENGINE")
        self.loop_log(f"   Active Target: Addr {addr_str} | Len {len_str} Bytes")
        self.loop_log(f"   Op Mode: {op_mode} | Pattern #{pat_idx} (0x{custom_hex})")
        if "Write" in op_mode and "Erase" not in op_mode:
            self.loop_log("   ⚠️ Write Pattern ไม่ได้ Erase ก่อน — flash เปลี่ยน 1→0 ได้อย่างเดียว ผลอาจเพี้ยนถ้ายังไม่ได้ลบ")
        self.loop_log("==========================================")

        try:
            while self.loop_running and not self.closing:
                self.loop_current_iter += 1
                max_str = str(max_count) if is_fixed_count else "∞"
                self.lbl_stat_iter.config(text=f"{self.loop_current_iter} / {max_str}")
                self.update_phase_status(f"[ Loop #{self.loop_current_iter}/{max_str} ]")

                is_pass = False
                fail_reason = ""

                if "Verify Pattern" in op_mode:
                    self.set_verified(pat_idx, custom_hex, t_addr, t_len)
                    resp = self.execute_cmd_with_progress(f"VERIFY:{addr_str}:{len_str}:{pat_idx}:{custom_hex}", ["VERIFY:OK", "VERIFY:FAIL:"])
                    if resp == "VERIFY:OK":
                        is_pass = True
                        self.mark_tested(t_addr, t_len)
                    elif resp and resp.startswith("VERIFY:FAIL:"):
                        self.mark_tested(t_addr, t_len)
                        cnt, where = self.parse_verify_fail(resp)
                        fail_reason = f"VERIFY FAIL ({cnt} errors) @ {where}"
                    else:
                        fail_reason = "no valid response (timeout/abort/error)"

                elif "Write Pattern" in op_mode:
                    self.clear_verified()
                    resp = self.execute_cmd_with_progress(f"WRITE_PAT:{addr_str}:{len_str}:{pat_idx}:{custom_hex}", ["WRITE_PAT:OK"])
                    if resp == "WRITE_PAT:OK":
                        is_pass = True
                    else:
                        fail_reason = "WRITE failed (no valid response)"

                elif "Erase -> Write -> Verify" in op_mode:
                    # ลบให้ครบทุก sector ที่ target คร่อมอยู่
                    erase_ok = True
                    for s in range(first_sec, last_sec + 1):
                        if not self.loop_running:
                            erase_ok = False
                            break
                        r = self.execute_cmd_with_progress(f"ERASE:0x{s * 4096:06X}", ["ERASE:OK"])
                        # firmware ตอบ "ERASE:OK:0xADDR" -> ต้องใช้ startswith
                        if not (r and r.startswith("ERASE:OK")):
                            erase_ok = False
                            fail_reason = f"ERASE failed @ sector {s}"
                            break
                    if erase_ok:
                        self.clear_verified()
                        resp_write = self.execute_cmd_with_progress(f"WRITE_PAT:{addr_str}:{len_str}:{pat_idx}:{custom_hex}", ["WRITE_PAT:OK"])
                        if resp_write == "WRITE_PAT:OK":
                            self.set_verified(pat_idx, custom_hex, t_addr, t_len)
                            resp_ver = self.execute_cmd_with_progress(f"VERIFY:{addr_str}:{len_str}:{pat_idx}:{custom_hex}", ["VERIFY:OK", "VERIFY:FAIL:"])
                            if resp_ver == "VERIFY:OK":
                                is_pass = True
                                self.mark_tested(t_addr, t_len)
                            elif resp_ver and resp_ver.startswith("VERIFY:FAIL:"):
                                self.mark_tested(t_addr, t_len)
                                cnt, where = self.parse_verify_fail(resp_ver)
                                fail_reason = f"ERASE-WRITE-VERIFY FAIL ({cnt} errors) @ {where}"
                            else:
                                fail_reason = "VERIFY no valid response (timeout/abort/error)"
                        else:
                            fail_reason = "WRITE failed (no valid response)"

                if self.closing:
                    break

                # สาย USB หลุด -> หยุดทันที (ไม่วนต่อ ไม่นับเป็น fail)
                if not self.is_connected():
                    self.loop_log(f"🔌 [STOPPED] Serial disconnected during Loop #{self.loop_current_iter}")
                    break

                # ผู้ใช้กด STOP -> ไม่นับเป็น Fail
                if not self.loop_running:
                    self.loop_log(f"⛔ [STOPPED] Loop #{self.loop_current_iter} aborted by user")
                    break

                if is_pass:
                    self.loop_pass_cnt += 1
                    self.lbl_stat_pass.config(text=str(self.loop_pass_cnt))
                    if self.loop_current_iter % 10 == 0 or self.loop_current_iter == 1:
                        self.loop_log(f"🟢 Loop #{self.loop_current_iter}: PASSED")
                else:
                    self.loop_fail_cnt += 1
                    self.lbl_stat_fail.config(text=str(self.loop_fail_cnt))
                    self.loop_log(f"❌ Loop #{self.loop_current_iter}: {fail_reason}")
                    if stop_on_fail:
                        self.loop_log(f"🛑 [STOPPED] Loop terminated on first failure @ Loop #{self.loop_current_iter}")
                        break

                if is_fixed_count and self.loop_current_iter >= max_count:
                    self.loop_log(f"✅ Target Loop count of {max_count} iterations completed!")
                    break

                if delay_ms > 0:
                    self.loop_sleep(delay_ms)
        finally:
            self.loop_running = False
            if not self.closing:
                self.update_progress(0)
                self.update_phase_status("[ State: IDLE ]")
                # ส่งผลไปแท็บ FA / Bitmap
                if does_verify:
                    self.update_fa_table_data()
                    self.render_bitmap_grid()
                    if self.failed_pages:
                        self.notebook.select(self.tab_fa)

    # =========================================================================
    # TAB 6: DATA I/O  (Dump / Compare / Blank Check)
    # -------------------------------------------------------------------------
    # ใช้เฉพาะคำสั่งที่ firmware มีอยู่แล้ว:
    #   READ:0xADDR:LEN                -> DATA:<hex bytes>
    #   VERIFY / VERIFY_FULL_PAT       -> VERIFY:OK|VERIFY:FAIL:
    # =========================================================================
    def setup_tab_io(self):
        file_frame = ttk.LabelFrame(self.tab_io, text=" 📁 Binary File (.bin) ")
        file_frame.pack(fill="x", padx=10, pady=5)
        self.ent_io_file = ttk.Entry(file_frame, width=95)
        self.ent_io_file.pack(side="left", padx=6, pady=6)
        ttk.Button(file_frame, text="Browse...", command=self.cmd_io_browse).pack(side="left", padx=4, pady=6)

        rng_frame = ttk.LabelFrame(self.tab_io, text=" 🎯 Range ")
        rng_frame.pack(fill="x", padx=10, pady=5)
        r1 = ttk.Frame(rng_frame)
        r1.pack(fill="x", padx=6, pady=4)

        ttk.Label(r1, text="Address (Hex):").pack(side="left", padx=4)
        self.ent_io_addr = ttk.Entry(r1, width=12)
        self.ent_io_addr.insert(0, "0x000000")
        self.ent_io_addr.pack(side="left", padx=4)
        self.ent_io_addr.bind("<KeyRelease>", self.on_io_addr_input)

        ttk.Label(r1, text="Length (Bytes):").pack(side="left", padx=(12, 4))
        self.ent_io_len = ttk.Entry(r1, width=12)
        self.ent_io_len.insert(0, "4096")
        self.ent_io_len.pack(side="left", padx=4)
        self.ent_io_len.bind("<KeyRelease>", self.on_io_len_input)

        self.btn_io_full = ttk.Button(r1, text=f"Full Chip ({self.chip_label})", command=self.cmd_io_full_chip)
        self.btn_io_full.pack(side="left", padx=8)

        self.lbl_io_sec_page = ttk.Label(r1, text="Sector: 0 (0-511)  |  Page: 0 (0-8191)", font=("Consolas", 9, "bold"), foreground="#007ACC")
        self.lbl_io_sec_page.pack(side="left", padx=10)

        ttk.Label(rng_frame,
                  text="💡 Dump / Blank Check ใช้ Address + Length  |  Compare ใช้ Address + ขนาดไฟล์ (ไม่ใช้ Length)",
                  font=("Segoe UI", 8, "italic"), foreground="#666666").pack(anchor="w", padx=10, pady=(0, 4))

        act_frame = ttk.LabelFrame(self.tab_io, text=" 🚀 Data Operations ")
        act_frame.pack(fill="x", padx=10, pady=5)
        a1 = ttk.Frame(act_frame)
        a1.pack(fill="x", padx=6, pady=6)

        ttk.Button(a1, text="📤 Dump Chip → .bin", command=self.cmd_io_dump).pack(side="left", padx=4)
        ttk.Button(a1, text="⚖️ Compare File vs Chip", command=self.cmd_io_compare).pack(side="left", padx=4)
        ttk.Button(a1, text="🧼 Blank Check", command=self.cmd_io_blank_check).pack(side="left", padx=4)

        ttk.Button(a1, text="🧹 Clear Log", command=self.cmd_io_clear_log).pack(side="right", padx=4)

        log_frame = ttk.LabelFrame(self.tab_io, text=" 📜 Data I/O Log ")
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)
        self.txt_io_log = scrolledtext.ScrolledText(log_frame, font=("Consolas", 9), state="disabled")
        self.txt_io_log.pack(fill="both", expand=True, padx=5, pady=5)

    # ---- UI helpers --------------------------------------------------------
    def io_log(self, text):
        if self.closing:
            return
        try:
            self.txt_io_log.config(state="normal")
            self.txt_io_log.insert(tk.END, text + "\n")
            self.txt_io_log.see(tk.END)
            self.txt_io_log.config(state="disabled")
        except tk.TclError:
            pass

    def cmd_io_clear_log(self):
        self.txt_io_log.config(state="normal")
        self.txt_io_log.delete('1.0', tk.END)
        self.txt_io_log.config(state="disabled")

    def cmd_io_browse(self):
        path = filedialog.askopenfilename(
            title="Select binary file",
            filetypes=[("Binary", "*.bin *.rom *.img"), ("All Files", "*.*")])
        if path:
            self.ent_io_file.delete(0, tk.END)
            self.ent_io_file.insert(0, path)

    def sync_target_to_io_ui(self):
        self.ent_io_addr.delete(0, tk.END)
        self.ent_io_addr.insert(0, f"0x{self.target_hex_addr:06X}")
        self.ent_io_len.delete(0, tk.END)
        self.ent_io_len.insert(0, str(self.target_len))
        self.update_target_info_labels()

    def on_io_addr_input(self, event=None):
        try:
            self.set_active_target(int(self.ent_io_addr.get().strip(), 16))
        except ValueError:
            pass

    def on_io_len_input(self, event=None):
        val = self.ent_io_len.get().strip()
        if val.isdigit():
            self.target_len = int(val)
            self.update_target_info_labels()

    def cmd_io_full_chip(self):
        self.set_active_target(0, self.chip_size)
        self.sync_target_to_io_ui()

    # ---- job control -------------------------------------------------------
    def io_begin(self):
        self.job_running = True
        self.stop_requested = False
        self.update_progress(0)

    def io_end(self):
        self.job_running = False
        if not self.closing:
            self.update_progress(0)
            self.update_phase_status("[ State: IDLE ]")

    def io_should_stop(self):
        """ เรียกระหว่าง chunk: ให้ UI ตอบสนอง (กด STOP ได้) แล้วเช็กว่าควรเลิกหรือไม่ """
        try:
            self.root.update()
        except tk.TclError:
            return True
        return self.stop_requested or self.closing or not self.is_connected()

    def io_fail(self, msg):
        """ แยก 'ผู้ใช้กด STOP' / 'สายหลุด' / 'error จริง' -> (state, message) """
        if not self.is_connected():
            return "error", "serial disconnected"
        if self.stop_requested:
            return "aborted", ""
        return "error", msg

    @staticmethod
    def fmt_eta(sec):
        sec = int(max(0, sec))
        return f"{sec // 60}:{sec % 60:02d}"

    def io_progress(self, name, done, total, t0):
        pct = int(done * 100 / total) if total else 100
        elapsed = time.time() - t0
        eta = elapsed * (total - done) / done if done else 0
        self.update_progress(pct)
        self.update_phase_status(f"[ {name} {pct}% | ETA {self.fmt_eta(eta)} ]")

    # ---- low-level flash access -------------------------------------------
    def io_read_block(self, addr, length):
        resp = self.execute_cmd_with_progress(f"READ:0x{addr:06X}:{length}", ["DATA:"], show_progress=False)
        if not resp or not resp.startswith("DATA:"):
            return None
        try:
            data = bytes.fromhex(resp[5:].strip())
        except ValueError:
            return None
        return data if len(data) == length else None

    def io_record_mismatches(self, base, expected, actual):
        """ เทียบ expected/actual แล้วบันทึกเข้า FA (แยกตาม page จริง) คืนจำนวน byte ที่ต่าง """
        if expected == actual:
            return 0
        total = 0
        n = min(len(expected), len(actual))
        o = 0
        while o < n:
            a0 = base + o
            seg = min(n - o, ((a0 >> 8) + 1) * 256 - a0)  # ไม่ข้ามขอบ page
            e = expected[o:o + seg]
            a = actual[o:o + seg]
            if e != a:
                diffs = [i for i in range(seg) if e[i] != a[i]]
                first = diffs[0]
                total += len(diffs)
                self.record_fail_line(f"FAIL_ADDR:{a0 + first:06X}:{len(diffs)}:exp {e[first]:02X} got {a[first]:02X}")
            o += seg
        return total

    def io_show_fa_results(self):
        if self.closing:
            return
        self.update_fa_table_data()
        self.render_bitmap_grid()
        if self.failed_pages:
            self.notebook.select(self.tab_fa)

    # ---- 1) DUMP -----------------------------------------------------------
    def cmd_io_dump(self):
        if not self.check_jedec_requirement():
            return
        if not self.read_target_entries(self.ent_io_addr, self.ent_io_len):
            return
        addr, length = self.target_hex_addr, self.target_len

        ts = time.strftime("%Y%m%d_%H%M%S")
        path = filedialog.asksaveasfilename(
            defaultextension=".bin",
            initialfile=f"dump_0x{addr:06X}_{length}B_{ts}.bin",
            filetypes=[("Binary", "*.bin"), ("All Files", "*.*")],
            title="Save Flash Dump")
        if not path:
            return

        tmp = path + ".part"  # เขียนลงไฟล์ชั่วคราวก่อน กันได้ไฟล์ dump ที่ไม่ครบแต่ดูเหมือนสมบูรณ์
        sha = hashlib.sha256()
        state, err = "ok", ""
        t0 = time.time()
        self.io_log("=" * 42)
        self.io_log(f"📤 DUMP 0x{addr:06X} - 0x{addr + length - 1:06X} ({length:,} B) -> {os.path.basename(path)}")
        self.io_begin()
        try:
            with open(tmp, "wb") as f:
                done = 0
                while done < length:
                    if self.io_should_stop():
                        state, err = self.io_fail("stopped")
                        if state == "ok":
                            state = "aborted"
                        break
                    n = min(IO_READ_CHUNK, length - done)
                    blk = self.io_read_block(addr + done, n)
                    if blk is None:
                        state, err = self.io_fail(f"read failed @ 0x{addr + done:06X}")
                        break
                    f.write(blk)
                    sha.update(blk)
                    done += n
                    self.io_progress("DUMP", done, length, t0)
        except OSError as e:
            state, err = "error", f"file error: {e}"
        finally:
            self.io_end()

        if state == "ok":
            try:
                os.replace(tmp, path)
            except OSError as e:
                state, err = "error", f"cannot save file: {e}"
        if state != "ok":
            try:
                os.remove(tmp)
            except OSError:
                pass

        dt = time.time() - t0
        if state == "ok":
            self.io_log(f"✅ Dump complete in {dt:.1f}s | SHA-256: {sha.hexdigest()}")
            if not self.closing:
                messagebox.showinfo("Dump complete", f"บันทึกแล้ว: {path}\nSHA-256:\n{sha.hexdigest()}")
        elif state == "aborted":
            self.io_log("[ABORTED] Dump stopped by user (ไม่ได้บันทึกไฟล์)")
        else:
            self.io_log(f"❌ Dump failed: {err}")

    # ---- 2) COMPARE --------------------------------------------------------
    def cmd_io_compare(self):
        if not self.check_jedec_requirement():
            return
        path = self.ent_io_file.get().strip()
        if not path:
            self.cmd_io_browse()
            path = self.ent_io_file.get().strip()
            if not path:
                return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            messagebox.showerror("Error", f"Cannot read file: {e}")
            return
        size = len(data)
        if size == 0:
            messagebox.showerror("Error", "ไฟล์ว่าง (0 bytes)")
            return
        if not self.read_target_entries(self.ent_io_addr, None):
            return
        base = self.target_hex_addr
        if base + size > self.chip_size:
            messagebox.showerror("Error", f"ไฟล์ {size:,} bytes ที่ 0x{base:06X} เกินขนาด chip ({self.chip_label})")
            return

        state, err = "ok", ""
        mismatch_bytes = 0
        t0 = time.time()
        self.io_log("=" * 42)
        self.io_log(f"⚖️ COMPARE {os.path.basename(path)} ({size:,} B) vs chip @ 0x{base:06X}")
        self.io_begin()
        self.clear_fa_data()
        self.clear_verified()
        try:
            done = 0
            while done < size:
                if self.io_should_stop():
                    state, err = self.io_fail("stopped")
                    if state == "ok":
                        state = "aborted"
                    break
                n = min(IO_READ_CHUNK, size - done)
                blk = self.io_read_block(base + done, n)
                if blk is None:
                    state, err = self.io_fail(f"read failed @ 0x{base + done:06X}")
                    break
                mismatch_bytes += self.io_record_mismatches(base + done, data[done:done + n], blk)
                self.mark_tested(base + done, n)
                done += n
                self.io_progress("COMPARE", done, size, t0)
        finally:
            self.io_end()

        dt = time.time() - t0
        if state == "ok":
            if mismatch_bytes:
                self.io_log(f"🔴 DIFFERENT: {mismatch_bytes:,} mismatched bytes in {len(self.failed_pages)} page(s) "
                            f"({dt:.1f}s) — ดูแท็บ Failure Analysis")
            else:
                self.io_log(f"🟢 IDENTICAL: ไฟล์ตรงกับชิปทุก byte ({dt:.1f}s)")
        elif state == "aborted":
            self.io_log("[ABORTED] Compare stopped by user (ผลเป็นเพียงบางส่วน)")
        else:
            self.io_log(f"❌ Compare failed: {err}")

        if self.tested_sectors or self.failed_pages:
            self.io_show_fa_results()

    # ---- 3) BLANK CHECK ----------------------------------------------------
    def cmd_io_blank_check(self):
        if not self.check_jedec_requirement():
            return
        if not self.read_target_entries(self.ent_io_addr, self.ent_io_len):
            return
        addr, length = self.target_hex_addr, self.target_len
        full = (addr == 0 and length == self.chip_size)

        self.io_log("=" * 42)
        self.io_log(f"🧼 BLANK CHECK 0x{addr:06X} - 0x{addr + length - 1:06X} ({length:,} B) — expect all 0xFF")
        self.io_begin()
        self.clear_fa_data()
        self.set_verified(0, "FF", addr, length)  # Hex Viewer จะไฮไลต์ byte ที่ไม่ใช่ 0xFF
        t0 = time.time()
        try:
            self.update_phase_status("[ Phase: BLANK CHECK... ]")
            cmd = "VERIFY_FULL_PAT:0:FF" if full else f"VERIFY:0x{addr:06X}:{length}:0:FF"
            resp = self.execute_cmd_with_progress(cmd, ["VERIFY:OK", "VERIFY:FAIL:"])
        finally:
            self.io_end()

        dt = time.time() - t0
        if resp == "VERIFY:OK":
            self.mark_tested(addr, length)
            self.io_log(f"🟢 BLANK: ทุก byte เป็น 0xFF ({dt:.2f}s)")
        elif resp and resp.startswith("VERIFY:FAIL:"):
            self.mark_tested(addr, length)
            cnt, where = self.parse_verify_fail(resp)
            self.io_log(f"🔴 NOT BLANK: {cnt} bytes ไม่ใช่ 0xFF — แรกสุดที่ {where} ({dt:.2f}s)")
        else:
            self.io_log("❌ Blank check: no valid response (timeout/abort/error)")

        if self.tested_sectors or self.failed_pages:
            self.io_show_fa_results()

    # =========================================================================
    # SYSTEM OPERATIONS & HANDLERS
    # =========================================================================
    def ops_log(self, text):
        if self.closing:
            return
        try:
            self.txt_ops_log.config(state="normal")
            self.txt_ops_log.insert(tk.END, text + "\n")
            self.txt_ops_log.see(tk.END)
            self.txt_ops_log.config(state="disabled")
        except tk.TclError:
            pass

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
        if self.is_connected():
            if self.busy or self.loop_running or self.job_running:
                self.ops_log("[SYSTEM] ⚠️ Cannot change SPI frequency while an operation is running.")
                return
            freq_str = self.freq_cb.get().split(" ")[0]
            resp = self.send_raw_cmd(f"SET_FREQ:{freq_str}")
            if resp and resp.startswith("FREQ:OK"):
                self.ops_log(f"[SYSTEM] SPI Frequency changed to {freq_str} MHz")

    def toggle_connection(self):
        if self.is_connected():
            if self.busy or self.job_running:
                messagebox.showwarning("Busy", "มีงานกำลังทำงานอยู่ กรุณากด STOP ก่อนตัดการเชื่อมต่อ")
                return
            self.stop_requested = True
            self.loop_running = False
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
            self.set_led_status(False)
        else:
            port = self.port_cb.get()
            if not port:
                messagebox.showerror("Error", "No COM Port selected!")
                return
            try:
                # ตั้ง DTR/RTS ก่อนเปิดพอร์ต เพื่อไม่ให้ ESP32 ถูก reset ตอนเปิด
                self.ser = serial.Serial()
                self.ser.port = port
                self.ser.baudrate = 921600
                self.ser.timeout = 3
                self.ser.dtr = False
                self.ser.rts = False
                self.ser.open()
                time.sleep(1.8)
                self.ser.reset_input_buffer()
                self.set_led_status(True)
                self.on_freq_changed()
            except Exception as e:
                self.ser = None
                messagebox.showerror("Error", f"Failed to connect: {e}")
                self.set_led_status(False)

    def send_raw_cmd(self, command):
        try:
            self.ser.reset_input_buffer()
            self.ser.write((command + "\n").encode('utf-8'))
            return self.ser.readline().decode('utf-8', errors='ignore').strip()
        except Exception:
            return None

    def execute_cmd_with_progress(self, command, expected_end_tokens, timeout=60.0, show_progress=True):
        """ timeout เป็น idle timeout: นับจากบรรทัดล่าสุดที่ได้รับ (PROGRESS ที่ส่งมาเรื่อยๆ ถือว่ายังไม่ค้าง) """
        if not self.is_connected():
            messagebox.showwarning("Warning", "Please connect to COM Port first!")
            return None
        if self.busy:
            self.ops_log("[WARN] Another command is still running.")
            return None

        self.busy = True
        self.stop_requested = False
        if show_progress:
            self.update_progress(0)

        ser = self.ser
        old_timeout = ser.timeout
        final_resp = None
        buf = bytearray()  # สะสมข้อมูลจนครบบรรทัด
        try:
            ser.reset_input_buffer()
            ser.write((command + "\n").encode('utf-8'))

            ser.timeout = 0.1
            start_time = time.time()

            while True:
                self.root.update()
                if self.stop_requested:
                    # cmd_stop ส่ง STOP ไปแล้ว ไม่ส่งซ้ำ แต่รอ ABORTED แล้วเคลียร์ buffer
                    self.ops_log("[ABORTED] Operation stopped by user!")
                    self.drain_after_stop()
                    self.update_progress(0)
                    self.update_phase_status("[ State: IDLE ]")
                    break

                if time.time() - start_time > timeout:
                    self.ops_log("[ERROR] Operation timed out (no data from ESP32)! Sending STOP...")
                    try:
                        ser.write(b"STOP\n")  # สั่ง ESP32 หยุดด้วย ไม่ปล่อยให้ค้างทำงานต่อ
                    except Exception:
                        pass
                    self.drain_after_stop()
                    break

                line = self.read_line(ser, buf)
                if not line:
                    continue
                start_time = time.time()  # มีข้อมูลเข้า = ยังไม่ค้าง (idle timeout)

                if line.startswith("PROGRESS:"):
                    if not show_progress:
                        continue
                    try:
                        pct = int(line.split(":")[1])
                        self.update_progress(pct)
                    except (ValueError, IndexError):
                        pass
                elif line.startswith("FAIL_ADDR:"):
                    self.record_fail_line(line)
                elif line == "ABORTED":
                    self.ops_log("[ABORTED] Hardware operation canceled.")
                    self.update_progress(0)
                    self.update_phase_status("[ State: IDLE ]")
                    break
                elif line.startswith("ERR"):
                    # firmware ตอบ error -> เลิกรอทันที ไม่รอจน timeout
                    self.ops_log(f"[FIRMWARE ERROR] {line}")
                    break
                else:
                    for token in expected_end_tokens:
                        if line.startswith(token) or line == token:
                            final_resp = line
                            break
                    if final_resp:
                        break

            return final_resp
        except Exception as e:
            # สาย USB หลุด / พอร์ตพัง -> หยุด loop + ปิดพอร์ต + เคลียร์ self.ser
            self.handle_disconnect(e)
            return None
        finally:
            self.busy = False
            try:
                ser.timeout = old_timeout
            except Exception:
                pass

    def get_custom_hex_val(self):
        return self.normalize_hex_byte(self.ent_custom_hex.get(), "AA")

    def run_all_pattern_sweep_engine(self):
        if not self.check_jedec_requirement():
            return
        if not self.is_connected():
            messagebox.showwarning("Warning", "Please connect to COM Port first!")
            return
        if not self.check_unlocked():
            return

        is_loop = 0
        self.stop_requested = False
        self.busy = True

        self.clear_fa_data()
        self.clear_verified()
        sweep_custom = SWEEP_CUSTOM_HEX  # firmware ฮาร์ดโค้ด pattern 4 = 0xA5

        self.ops_log("\n==========================================")
        self.ops_log(f"🚀 STARTING ALL-PATTERN FULL-CHIP SWEEP ({self.chip_label})")
        self.ops_log("==========================================")

        ser = self.ser
        old_timeout = ser.timeout
        buf = bytearray()
        try:
            ser.reset_input_buffer()
            ser.write(f"ALL_PAT_START:{is_loop}\n".encode('utf-8'))

            pat_names = ["0xFF", "0x00", "0xAA", "0x55", f"Custom (0x{sweep_custom})", "Pseudo-Random", "Alt 0xAA/0x55", "Inc (0x00-0xFF)"]
            curr_cycle = 1
            curr_pat = 0
            curr_phase = "START"

            ser.timeout = 0.1

            while not self.stop_requested:
                self.root.update()
                line = self.read_line(ser, buf)
                if not line:
                    continue

                if line.startswith("SWEEP_CYCLE_START:"):
                    curr_cycle = int(line.split(":")[1])
                    self.ops_log(f"\n🔄 --- Sweep Cycle #{curr_cycle} ---")

                elif line.startswith("PAT_START:"):
                    curr_pat = int(line.split(":")[1])
                    self.set_verified(curr_pat, sweep_custom, 0, self.chip_size)
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
                    except (ValueError, IndexError):
                        pass

                elif line.startswith("FAIL_ADDR:"):
                    self.record_fail_line(line)

                elif line.startswith("PAT_DONE:"):
                    parts = line.split(":")
                    pat_id = int(parts[1])
                    p_name = pat_names[pat_id] if pat_id < len(pat_names) else f"#{pat_id}"
                    self.ops_log(f"  🟢 Pat #{pat_id} ({p_name}) Phase Done")

                elif line.startswith("SWEEP_CYCLE_DONE:"):
                    c_num = line.split(":")[1]
                    self.ops_log(f"✅ Sweep Cycle #{c_num} Completed!")
                    self.tested_sectors.update(range(self.n_sectors))  # ทั้งชิปถูกทดสอบแล้ว
                    break

                elif line == "ABORTED":
                    self.ops_log("[ABORTED] Sweep operation canceled by user.")
                    break

                elif line.startswith("ERR"):
                    self.ops_log(f"[FIRMWARE ERROR] {line}")
                    break

            if self.stop_requested:
                self.ops_log("[ABORTED] Sweep operation stopped by user.")
                self.drain_after_stop()
        except Exception as e:
            self.handle_disconnect(e)
        finally:
            self.busy = False
            try:
                ser.timeout = old_timeout
            except Exception:
                pass

        if self.closing:
            return
        self.update_progress(0)
        self.update_phase_status("[ State: IDLE ]")

        self.update_fa_table_data()
        self.render_bitmap_grid()
        if self.failed_pages:
            self.notebook.select(self.tab_fa)

    def cmd_standalone_erase_chip(self):
        if not self.check_jedec_requirement():
            return
        if not self.check_unlocked():
            return
        if messagebox.askyesno("Confirm Chip Erase", f"Are you sure you want to erase the ENTIRE CHIP ({self.chip_label})?"):
            self.update_phase_status("[ Phase: ERASING CHIP... ]")
            self.ops_log(f"[ACTION] Erasing ENTIRE CHIP ({self.chip_label})... Please wait...")
            resp = self.execute_cmd_with_progress("ERASE_CHIP", ["ERASE_CHIP:OK"])
            if resp == "ERASE_CHIP:OK":
                self.clear_verified()
                self.ops_log("[SUCCESS] 💥 Entire Chip Erased Successfully!")
            self.update_phase_status("[ State: IDLE ]")

    def cmd_execute_action(self):
        if not self.check_jedec_requirement():
            return

        mode = self.op_mode_cb.get()
        scope = self.scope_cb.get()
        is_entire = "Entire" in scope

        if "Sweep" in mode:
            self.run_all_pattern_sweep_engine()
            return

        # อ่าน + validate จากช่องกรอกก่อนสั่งงาน (Erase Sector ใช้เฉพาะ address)
        if "Erase Sector" in mode:
            if not self.read_target_entries(self.ent_addr, None):
                return
        elif not is_entire:
            if not self.read_target_entries(self.ent_addr, self.ent_len):
                return

        # เก็บค่า target เป็นตัวแปร local (ระหว่างรอ root.update() ผู้ใช้อาจแก้ช่องกรอกได้)
        t_addr = self.target_hex_addr
        t_len = self.target_len
        addr = f"0x{t_addr:06X}"
        length = str(t_len)
        pat_idx = int(self.pat_cb.get().split(":")[0])
        custom_hex = self.get_custom_hex_val()

        if "Write Pattern" in mode:
            if not self.check_unlocked():
                return
            self.update_phase_status("[ Phase: WRITING... ]")
            self.clear_verified()  # ข้อมูลเปลี่ยนแล้ว ไม่ให้ Hex Viewer ไฮไลต์ตาม pattern เก่า
            self.ops_log("[NOTE] ⚠️ Write Pattern ไม่ได้ Erase ก่อน — flash เปลี่ยน 1→0 ได้อย่างเดียว ถ้ายังไม่ลบ ผลอาจเพี้ยน")
            if is_entire:
                self.ops_log(f"[ACTION] Writing Pattern #{pat_idx} to ENTIRE CHIP ({self.chip_label})...")
                t0 = time.time()
                resp = self.execute_cmd_with_progress(f"WRITE_FULL_PAT:{pat_idx}:{custom_hex}", ["WRITE_PAT:OK"])
                t1 = time.time()
                if resp == "WRITE_PAT:OK":
                    self.ops_log(f"[SUCCESS] 🚀 Full Chip Written in {t1 - t0:.2f}s!")
            else:
                self.ops_log(f"[ACTION] Writing Pattern #{pat_idx} to {addr} (Len: {length} bytes)...")
                t0 = time.time()
                resp = self.execute_cmd_with_progress(f"WRITE_PAT:{addr}:{length}:{pat_idx}:{custom_hex}", ["WRITE_PAT:OK"])
                t1 = time.time()
                if resp == "WRITE_PAT:OK":
                    self.ops_log(f"[SUCCESS] Pattern Written in {t1 - t0:.3f}s!")
            self.update_phase_status("[ State: IDLE ]")

        elif "Verify Pattern" in mode:
            self.update_phase_status("[ Phase: VERIFYING... ]")
            self.clear_fa_data()

            if is_entire:
                self.set_verified(pat_idx, custom_hex, 0, self.chip_size)
                self.ops_log(f"[ACTION] Verifying ENTIRE CHIP with Pattern #{pat_idx}...")
                t0 = time.time()
                resp = self.execute_cmd_with_progress(f"VERIFY_FULL_PAT:{pat_idx}:{custom_hex}", ["VERIFY:OK", "VERIFY:FAIL:"])
                t1 = time.time()
                if resp == "VERIFY:OK":
                    self.ops_log(f"[PASSED] 🟢 Full Chip Verification PASSED in {t1 - t0:.2f}s!")
                elif resp and resp.startswith("VERIFY:FAIL:"):
                    cnt, where = self.parse_verify_fail(resp)
                    self.ops_log(f"[FAILED] 🔴 Full Chip Verification FAILED! ({cnt} errors). First Mismatch @ {where}")
                if resp and (resp == "VERIFY:OK" or resp.startswith("VERIFY:FAIL:")):
                    self.tested_sectors.update(range(self.n_sectors))  # ทั้ง OK และ FAIL ถือว่าทดสอบทั้งชิปแล้ว
            else:
                self.set_verified(pat_idx, custom_hex, t_addr, t_len)
                self.ops_log(f"[ACTION] Verifying Pattern #{pat_idx} at {addr} (Len: {length} bytes)...")
                t0 = time.time()
                resp = self.execute_cmd_with_progress(f"VERIFY:{addr}:{length}:{pat_idx}:{custom_hex}", ["VERIFY:OK", "VERIFY:FAIL:"])
                t1 = time.time()
                if resp and (resp == "VERIFY:OK" or resp.startswith("VERIFY:FAIL:")):
                    self.mark_tested(t_addr, t_len)

                if resp == "VERIFY:OK":
                    self.ops_log(f"[PASSED] 🟢 Verification PASSED (0 Errors) in {t1 - t0:.3f}s!")
                elif resp and resp.startswith("VERIFY:FAIL:"):
                    cnt, where = self.parse_verify_fail(resp)
                    self.ops_log(f"[FAILED] 🔴 Verification FAILED! ({cnt} errors). First Mismatch @ {where}")

            self.update_phase_status("[ State: IDLE ]")

            self.update_fa_table_data()
            self.render_bitmap_grid()
            if self.failed_pages:
                self.notebook.select(self.tab_fa)

        elif "Erase Sector" in mode:
            if not self.check_unlocked():
                return
            self.update_phase_status("[ Phase: ERASING... ]")
            sec_addr = f"0x{(t_addr & ~0xFFF):06X}"
            self.ops_log(f"[ACTION] Erasing Sector at {sec_addr}...")
            resp = self.execute_cmd_with_progress(f"ERASE:{sec_addr}", ["ERASE:OK"])
            # firmware ตอบ "ERASE:OK:0xADDR" -> ต้องใช้ startswith
            if resp and resp.startswith("ERASE:OK"):
                self.clear_verified()
                self.ops_log(f"[SUCCESS] Sector {sec_addr} Erased.")
            self.update_phase_status("[ State: IDLE ]")

    def cmd_read_id(self):
        if not self.is_connected():
            messagebox.showwarning("Warning", "Please connect to COM Port first!")
            return
        if not self.ensure_idle():
            return
        resp = self.execute_cmd_with_progress("READ_ID", ["ID:"])
        if resp and resp.startswith("ID:"):
            id_str = resp[3:].strip()
            self.lbl_jedec.config(text=f"JEDEC ID: {id_str}")
            dev_name = JEDEC_DB.get(id_str)
            if dev_name is None:
                self.jedec_read_valid = False
                self.lbl_device.config(text="Device: Unknown / Unsupported (รองรับเฉพาะ SST26VF)", foreground="#FF3B30")
                if id_str in ("00 00 00", "FF FF FF"):
                    messagebox.showwarning("Warning", f"อ่านได้ค่า JEDEC ID ({id_str}) ซึ่งไม่ถูกต้อง กรุณาเช็กการเชื่อมต่อสาย IC!")
                else:
                    messagebox.showwarning("Warning", f"JEDEC ID {id_str} ไม่ใช่ SST26VF ที่เครื่องมือนี้รองรับ")
                return
            self.lbl_device.config(text=f"Device: {dev_name}", foreground="#444444")
            self.jedec_read_valid = True
            self.apply_chip_size(CHIP_BY_CAP[int(id_str.split()[2], 16)])
            self.ops_log(f"[SYSTEM] ✅ Check JEDEC ID Validated: {id_str} ({dev_name})")
        else:
            self.jedec_read_valid = False
            if self.is_connected():  # ถ้าหลุดการเชื่อมต่อ handle_disconnect แจ้งไปแล้ว
                messagebox.showerror("Error", "Failed to read JEDEC ID")

    def cmd_unlock(self):
        if not self.check_jedec_requirement():
            return
        resp = self.execute_cmd_with_progress("UNLOCK", ["UNLOCK:OK"])
        if resp == "UNLOCK:OK":
            messagebox.showinfo("Success", "Global Block Protection Unlocked!")
            self.update_lock_ui(True)
            self.cmd_read_regs()
        else:
            if self.is_connected():
                messagebox.showerror("Error", "Unlock failed (ดู Operation Log สำหรับรายละเอียดจาก firmware)")
            self.update_lock_ui(False)

    def cmd_read_regs(self):
        if not self.check_jedec_requirement():
            return
        resp = self.execute_cmd_with_progress("READ_REGS", ["REGS:"])
        parts = resp.split(":") if resp else []
        if resp and resp.startswith("REGS:") and len(parts) >= 3:  # กัน IndexError
            stat_hex, cfg_hex = parts[1], parts[2]
            self.lbl_stat_reg.config(text=f"SR(0x05): 0x{stat_hex}")
            self.lbl_cfg_reg.config(text=f"CR(0x35): 0x{cfg_hex}")
        else:
            if self.is_connected():
                messagebox.showerror("Error", "Failed to read registers")


if __name__ == "__main__":
    root = tk.Tk()
    app = FlashProgrammerGUI(root)
    root.mainloop()