import time
import tkinter as tk
from tkinter import ttk, messagebox
import serial
import serial.tools.list_ports

class CompactFlashGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("SST26VF016B Flash Programmer - ESP32")
        self.root.geometry("520x330")
        self.root.resizable(False, False)
        
        self.ser = None

        # --- Style & Theme ---
        style = ttk.Style()
        style.theme_use('clam')

        # --- Frame 1: Connection & Status ---
        conn_frame = ttk.LabelFrame(root, text=" Serial Connection ")
        conn_frame.pack(fill="x", padx=12, pady=8)

        ttk.Label(conn_frame, text="Port:").grid(row=0, column=0, padx=5, pady=8)
        self.port_cb = ttk.Combobox(conn_frame, width=12, state="readonly")
        self.port_cb.grid(row=0, column=1, padx=5, pady=8)
        
        btn_refresh = ttk.Button(conn_frame, text="Refresh", command=self.refresh_ports)
        btn_refresh.grid(row=0, column=2, padx=5, pady=8)

        self.btn_connect = ttk.Button(conn_frame, text="Connect", command=self.toggle_connection)
        self.btn_connect.grid(row=0, column=3, padx=5, pady=8)

        # ไฟสถานะ LED (Canvas Circle)
        self.led_canvas = tk.Canvas(conn_frame, width=20, height=20, highlightthickness=0)
        self.led_canvas.grid(row=0, column=4, padx=(15, 2), pady=8)
        self.led_circle = self.led_canvas.create_oval(3, 3, 17, 17, fill="#FF3B30", outline="#B30000") # สีแดงเริ่มต้น

        self.lbl_status = ttk.Label(conn_frame, text="Disconnected", font=("Segoe UI", 9, "bold"), foreground="#FF3B30")
        self.lbl_status.grid(row=0, column=5, padx=5, pady=8)

        # --- Frame 2: Device Operations ---
        dev_frame = ttk.LabelFrame(root, text=" Device Operations ")
        dev_frame.pack(fill="x", padx=12, pady=5)

        ttk.Button(dev_frame, text="Read JEDEC ID", command=self.cmd_read_id).grid(row=0, column=0, padx=10, pady=10)
        ttk.Button(dev_frame, text="Global Protection Unlock", command=self.cmd_unlock).grid(row=0, column=1, padx=10, pady=10)
        
        self.lbl_jedec = ttk.Label(dev_frame, text="JEDEC ID: -- -- --", font=("Consolas", 10, "bold"))
        self.lbl_jedec.grid(row=0, column=2, padx=15, pady=10)

        # --- Frame 3: Register Status ---
        reg_frame = ttk.LabelFrame(root, text=" Register Status ")
        reg_frame.pack(fill="x", padx=12, pady=5)

        ttk.Button(reg_frame, text="Read Registers", command=self.cmd_read_regs).grid(row=0, column=0, padx=10, pady=10)

        reg_info_frame = ttk.Frame(reg_frame)
        reg_info_frame.grid(row=0, column=1, padx=15, pady=5, sticky="w")

        self.lbl_stat_reg = ttk.Label(reg_info_frame, text="Status Reg (0x05): --  [ BUSY: - | WEL: - ]", font=("Consolas", 9))
        self.lbl_stat_reg.pack(anchor="w", pady=2)

        self.lbl_cfg_reg = ttk.Label(reg_info_frame, text="Config Reg (0x35): --  [ IOC: - | BPNV: - ]", font=("Consolas", 9))
        self.lbl_cfg_reg.pack(anchor="w", pady=2)

        self.refresh_ports()

    def set_led_status(self, connected):
        if connected:
            self.led_canvas.itemconfig(self.led_circle, fill="#34C759", outline="#1D8339") # สีเขียว
            self.lbl_status.config(text="Connected", foreground="#34C759")
            self.btn_connect.config(text="Disconnect")
        else:
            self.led_canvas.itemconfig(self.led_circle, fill="#FF3B30", outline="#B30000") # สีแดง
            self.lbl_status.config(text="Disconnected", foreground="#FF3B30")
            self.btn_connect.config(text="Connect")

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
                self.ser = serial.Serial(port, 115200, timeout=2)
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
            stat_hex = parts[1]
            cfg_hex = parts[2]
            
            stat_val = int(stat_hex, 16)
            cfg_val = int(cfg_hex, 16)

            busy = (stat_val >> 0) & 1
            wel  = (stat_val >> 1) & 1
            ioc  = (cfg_val >> 1) & 1
            bpnv = (cfg_val >> 3) & 1

            self.lbl_stat_reg.config(text=f"Status Reg (0x05): 0x{stat_hex}  [ BUSY: {busy} | WEL: {wel} ]")
            self.lbl_cfg_reg.config(text=f"Config Reg (0x35): 0x{cfg_hex}  [ IOC: {ioc} | BPNV: {bpnv} ]")
        else:
            messagebox.showerror("Error", "Failed to read registers")

if __name__ == "__main__":
    root = tk.Tk()
    app = CompactFlashGUI(root)
    root.mainloop()