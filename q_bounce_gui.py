import json
import os
import sys
import sysconfig
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageOps, ImageTk


DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("q_bounce_config.json")
CONFIG_PATH = (
    Path(sys.executable).with_name(DEFAULT_CONFIG_PATH.name)
    if getattr(sys, "frozen", False)
    else DEFAULT_CONFIG_PATH
)
TRANSPARENT = 255
MIN_FRAME_MS = 20


@dataclass(frozen=True)
class Settings:
    base_width_px: int
    pressed_height_percent: float
    pressed_width_ratio: float
    press_ms: int
    release_ms: int
    top_hold_ms: int
    bottom_hold_ms: int
    ease_start_percent: float
    ease_end_percent: float
    transition_frames: int


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    minimum: float
    maximum: float
    step: float


SHAPE = (
    Field("base_width_px", "原高时的宽度（像素）", 16, 4096, 1),
    Field("pressed_height_percent", "压到底的高度（%）", 1, 100, 0.5),
    Field("pressed_width_ratio", "压到底的宽度（倍）", 1, 8, 0.05),
)
TIMING = (
    Field("press_ms", "下压时长（毫秒）", MIN_FRAME_MS, 10000, 10),
    Field("release_ms", "上弹时长（毫秒）", MIN_FRAME_MS, 10000, 10),
    Field("top_hold_ms", "顶部停顿（毫秒）", 0, 10000, 10),
    Field("bottom_hold_ms", "底部停顿（毫秒）", 0, 10000, 10),
)
EASING = (
    Field("ease_start_percent", "起步加速距离（%）", 0, 100, 1),
    Field("ease_end_percent", "到位减速距离（%）", 0, 100, 1),
    Field("transition_frames", "每段最多过渡帧", 1, 120, 1),
)
GROUPS = (("形变", SHAPE), ("节奏", TIMING), ("缓冲和流畅度", EASING))
INTEGER_FIELDS = {"base_width_px", "transition_frames", *(field.key for field in TIMING)}


def parse_settings(values):
    parsed = {}
    for _, group in GROUPS:
        for field in group:
            value = values.get(field.key, "")
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{field.label}需要填写数字。") from None
            if not field.minimum <= number <= field.maximum:
                raise ValueError(f"{field.label}的范围是 {field.minimum:g}–{field.maximum:g}。")
            if field.key in INTEGER_FIELDS:
                if not number.is_integer():
                    raise ValueError(f"{field.label}需要填写整数。")
                number = int(number)
            if field.key.endswith("_ms") and (number % 10 or 0 < number < MIN_FRAME_MS):
                raise ValueError(f"{field.label}请填 10 的倍数，非零时至少为 20。")
            parsed[field.key] = number
    if parsed["ease_start_percent"] + parsed["ease_end_percent"] > 100:
        raise ValueError("起步和到位的缓冲距离相加最多为 100%。")
    return Settings(**parsed)


def load_settings(path=None):
    if path is None:
        path = CONFIG_PATH if CONFIG_PATH.exists() else DEFAULT_CONFIG_PATH
    values = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(values, dict):
        raise ValueError("配置文件需要是 JSON 对象。")
    return parse_settings(values)


def write_atomic(path, write):
    path = Path(path)
    with NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as temporary:
        temp_path = Path(temporary.name)
    try:
        write(temp_path)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def save_settings(settings, path=CONFIG_PATH):
    content = json.dumps(asdict(settings), ensure_ascii=False, indent=2) + "\n"
    write_atomic(path, lambda temporary: temporary.write_text(content, encoding="utf-8"))


def eased_progress(time, start_distance, end_distance):
    # The cubic portions meet the constant-speed middle with the same slope.
    speed = 1 + 2 * (start_distance + end_distance)
    start_time = 3 * start_distance / speed
    end_time = 3 * end_distance / speed
    if time < start_time:
        return start_distance * (time / start_time) ** 3
    if time > 1 - end_time:
        return 1 - end_distance * ((1 - time) / end_time) ** 3
    return min(1, max(0, start_distance + speed * (time - start_time)))


@dataclass(frozen=True)
class Frame:
    width: int
    height: int
    duration_ms: int


def animation_plan(source_size, settings):
    source_width, source_height = source_size
    base_width = settings.base_width_px
    base_height = max(1, round(source_height * base_width / source_width))
    wide_width = round(base_width * settings.pressed_width_ratio)
    low_height = max(1, round(base_height * settings.pressed_height_percent / 100))
    start_distance = settings.ease_start_percent / 100
    end_distance = settings.ease_end_percent / 100
    frames = []
    for descending, duration_ms, hold_ms in (
        (True, settings.press_ms, settings.top_hold_ms),
        (False, settings.release_ms, settings.bottom_hold_ms),
    ):
        if hold_ms:
            size = (base_width, base_height) if descending else (wide_width, low_height)
            frames.append(Frame(*size, hold_ms))
        count = min(settings.transition_frames, duration_ms // MIN_FRAME_MS)
        for index in range(count):
            # Midpoint samples fit between the two held poses. A phase without a
            # hold shows its endpoint in the first motion frame.
            sample = (index + (0.5 if hold_ms else 0)) / count
            progress = eased_progress(sample, start_distance, end_distance)
            if not descending:
                progress = 1 - progress
            width = round(base_width + (wide_width - base_width) * progress)
            height = base_height - round((base_height - low_height) * progress)
            start_ms = round(index * duration_ms / (10 * count)) * 10
            end_ms = round((index + 1) * duration_ms / (10 * count)) * 10
            frames.append(Frame(width, height, end_ms - start_ms))
    return (wide_width, base_height), frames


def load_image(path):
    with Image.open(path) as image:
        source = ImageOps.exif_transpose(image).convert("RGBA")
    if source.getchannel("A").getextrema()[1] < 128:
        raise ValueError("这张图片没有可见内容。")
    return source


def export_gif(source, settings, destination):
    canvas_size, plan = animation_plan(source.size, settings)
    canvas_width, canvas_height = canvas_size
    if max(canvas_size) > 65535 or canvas_width * canvas_height * len(plan) > 180_000_000:
        raise ValueError("动图尺寸过大，请降低输出宽度或过渡帧数。")

    sample = source.copy()
    sample.thumbnail((384, 384), Image.Resampling.LANCZOS)
    palette_source = Image.new("RGB", sample.size)
    palette_source.paste(sample.convert("RGB"), mask=sample.getchannel("A"))
    palette = palette_source.quantize(colors=255)
    colors = palette.getpalette()
    colors = (colors + [0] * 768)[:765] + colors[:3]
    palette.putpalette(colors)
    opaque_indices = list(range(255)) + [0]
    alpha_threshold = [0] * 128 + [255] * 128
    frames = []
    for frame in plan:
        photo = source.resize((frame.width, frame.height), Image.Resampling.LANCZOS)
        visible = photo.getchannel("A").point(alpha_threshold)
        indexed = photo.convert("RGB").quantize(palette=palette, dither=Image.Dither.NONE)
        if indexed.histogram()[TRANSPARENT]:
            indexed = indexed.point(opaque_indices)
        canvas = Image.new("P", canvas_size, TRANSPARENT)
        canvas.putpalette(colors)
        canvas.paste(indexed, ((canvas_width - frame.width) // 2, canvas_height - frame.height), visible)
        frames.append(canvas)

    def save(path):
        frames[0].save(
            path, format="GIF", save_all=True, append_images=frames[1:],
            duration=[frame.duration_ms for frame in plan], loop=0, disposal=2,
            transparency=TRANSPARENT, background=TRANSPARENT, optimize=False,
        )

    write_atomic(destination, save)
    return canvas_size, sum(frame.duration_ms for frame in plan)


def create_window():
    root = tk.Tk()
    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD
    except ImportError:
        return root, None, "拖入图片需要 tkinterdnd2。仍可点击「选择图片」。"

    # Some Windows launchers leave this unset. TkDND needs the architecture of
    # the running Python to select its bundled DLL.
    if os.name == "nt" and not os.environ.get("PROCESSOR_ARCHITECTURE"):
        architecture = {"win32": "x86", "win-amd64": "AMD64", "win-arm64": "ARM64"}
        if sysconfig.get_platform() in architecture:
            os.environ["PROCESSOR_ARCHITECTURE"] = architecture[sysconfig.get_platform()]
    try:
        TkinterDnD.require(root)
    except (AttributeError, RuntimeError, tk.TclError):
        return root, None, "拖放组件无法加载。仍可点击「选择图片」。"
    return root, DND_FILES, ""


class BounceApp:
    def __init__(self, root, settings, drop_format=None, drop_status=""):
        self.root = root
        root.title("Q 弹 GIF 制作器")
        root.geometry("880x740")
        root.minsize(720, 675)
        self.source = self.source_path = None
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.export_future = None
        self.refresh_job = self.animation_job = None
        self.photos = []
        self.variables = {}
        self.can_drop = False
        root.protocol("WM_DELETE_WINDOW", self.close)

        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("Muted.TLabel", foreground="#544c66")
        style.configure("Export.TButton", background="#5535a1", foreground="white", padding=(14, 8))
        style.map("Export.TButton", background=[("disabled", "#787283"), ("active", "#452780")])

        outer = ttk.Frame(root, padding=20)
        outer.pack(expand=True, fill="both")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)
        ttk.Label(outer, text="Q 弹 GIF 制作器", style="Title.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 14)
        )

        left = ttk.Frame(outer)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 16))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        pick_row = ttk.Frame(left)
        pick_row.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(pick_row, text="选择图片…", command=self.choose_image).pack(side="left")
        self.filename = ttk.Label(pick_row, text="未选择图片", style="Muted.TLabel", width=28)
        self.filename.pack(side="left", padx=8)
        self.preview = tk.Canvas(left, background="#f0edf6", highlightthickness=0, width=360, height=450)
        self.preview.grid(row=1, column=0, sticky="nsew")
        self.preview.bind("<Configure>", self.queue_preview)
        self.preview.bind("<Double-Button-1>", lambda event: self.choose_image())
        name_row = ttk.Frame(left)
        name_row.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(name_row, text="输出文件名").pack(side="left", padx=(0, 8))
        self.output_name = tk.StringVar()
        self.output_entry = ttk.Entry(name_row, textvariable=self.output_name)
        self.output_entry.pack(side="left", fill="x", expand=True)
        self.output_entry.state(["disabled"])
        self.output_entry.bind("<Return>", lambda event: self.export())

        controls = ttk.Frame(outer)
        controls.grid(row=1, column=1, sticky="nsew")
        for label, fields in GROUPS:
            group = ttk.LabelFrame(controls, text=label, padding=(10, 6))
            group.pack(fill="x", pady=(0, 8))
            group.columnconfigure(0, weight=1)
            for row, field in enumerate(fields):
                ttk.Label(group, text=field.label).grid(row=row, column=0, sticky="w", pady=2)
                variable = tk.StringVar(value=f"{getattr(settings, field.key):g}")
                self.variables[field.key] = variable
                control = ttk.Spinbox(
                    group, from_=field.minimum, to=field.maximum,
                    increment=field.step, textvariable=variable, width=9,
                )
                control.grid(row=row, column=1, padx=(12, 0), pady=2)
                variable.trace_add("write", self.queue_preview)
        self.timing = ttk.Label(controls, style="Muted.TLabel", wraplength=280)
        self.timing.pack(fill="x", pady=(1, 8))
        ttk.Button(controls, text="保存配置", command=self.save_configuration).pack(anchor="w")

        actions = ttk.Frame(outer)
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        self.export_button = ttk.Button(
            actions, text="导出 GIF…", command=self.export, style="Export.TButton",
        )
        self.export_button.pack(side="right")
        self.export_button.state(["disabled"])
        self.status = ttk.Label(actions, style="Muted.TLabel", wraplength=510)
        self.status.pack(side="left", fill="x", expand=True, padx=(0, 12))
        if drop_format:
            try:
                for target in (self.preview, outer):
                    target.drop_target_register(drop_format)
                    target.dnd_bind("<<Drop>>", self.drop_image)
                self.can_drop = True
            except tk.TclError:
                drop_status = "拖放组件无法注册。仍可点击「选择图片」。"
        if drop_status:
            self.status.configure(text=drop_status)
        self.queue_preview()

    def settings(self):
        return parse_settings({key: variable.get() for key, variable in self.variables.items()})

    def choose_image(self):
        path = filedialog.askopenfilename(
            parent=self.root, title="选择图片",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.bmp *.gif *.tif *.tiff"), ("所有文件", "*.*")],
        )
        if path:
            self.open_image(Path(path))

    def drop_image(self, event):
        paths = self.root.tk.splitlist(event.data)
        if len(paths) != 1:
            self.status.configure(text="请一次拖入一张图片。")
            return "refuse_drop"
        # Let the OS finish the drag before opening files or focusing an entry.
        self.root.after_idle(self.open_image, Path(paths[0]))
        return "copy"

    def open_image(self, path):
        try:
            source = load_image(path)
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            messagebox.showerror("无法打开图片", str(error), parent=self.root)
            return False
        self.source, self.source_path = source, path
        self.filename.configure(text=path.name)
        self.output_name.set(f"{path.stem}_Q弹.gif")
        self.output_entry.state(["!disabled"])
        self.output_entry.focus_set()
        self.output_entry.selection_range(0, len(self.output_name.get()) - 4)
        self.status.configure(text=f"已载入图片 · {source.width} × {source.height} · 可修改文件名后导出")
        if self.export_future is None:
            self.export_button.state(["!disabled"])
        self.queue_preview()
        return True

    def queue_preview(self, *_):
        if self.refresh_job is not None:
            self.root.after_cancel(self.refresh_job)
        self.refresh_job = self.root.after(100, self.refresh_preview)

    def refresh_preview(self):
        self.refresh_job = None
        try:
            settings = self.settings()
        except ValueError as error:
            self.timing.configure(text=str(error))
            return
        cycle = settings.press_ms + settings.release_ms + settings.top_hold_ms + settings.bottom_hold_ms
        self.timing.configure(
            text=f"下压速度为上弹的 {settings.release_ms / settings.press_ms:.2f} 倍 · {cycle / 1000:g} 秒/轮"
        )
        if self.animation_job is not None:
            self.root.after_cancel(self.animation_job)
            self.animation_job = None
        self.preview.delete("all")
        width, height = self.preview.winfo_width(), self.preview.winfo_height()
        if self.source is None:
            prompt = "把图片拖到这里" if self.can_drop else "选择一张图片"
            self.preview.create_text(width // 2, height // 2, text=prompt, fill="#4f4264")
            return
        (canvas_width, canvas_height), plan = animation_plan(self.source.size, settings)
        scale = min(max(1, width - 32) / canvas_width, max(1, height - 32) / canvas_height)
        preview_source = self.source.copy()
        preview_source.thumbnail((max(1, width), max(1, height)), Image.Resampling.LANCZOS)
        self.photos = []
        cache = {}
        for frame in plan:
            size = (max(1, round(frame.width * scale)), max(1, round(frame.height * scale)))
            if size not in cache:
                photo = preview_source.resize(size, Image.Resampling.LANCZOS)
                cache[size] = ImageTk.PhotoImage(photo, master=self.root)
            self.photos.append(cache[size])
        bottom = (height + round(canvas_height * scale)) // 2
        self.preview_item = self.preview.create_image(width // 2, bottom, anchor="s")
        self.show_frame(plan, 0)

    def show_frame(self, plan, index):
        self.preview.itemconfigure(self.preview_item, image=self.photos[index])
        self.animation_job = self.root.after(
            plan[index].duration_ms, self.show_frame, plan, (index + 1) % len(plan)
        )

    def save_configuration(self):
        try:
            save_settings(self.settings())
        except (OSError, ValueError) as error:
            messagebox.showerror("未能保存配置", str(error), parent=self.root)
            return
        self.status.configure(text=f"已保存 {CONFIG_PATH.name}")

    def export(self):
        if self.source is None or self.export_future is not None:
            return
        try:
            settings = self.settings()
        except ValueError as error:
            messagebox.showerror("请检查参数", str(error), parent=self.root)
            return
        filename = Path(self.output_name.get().strip()).name or f"{self.source_path.stem}_Q弹.gif"
        name = filedialog.asksaveasfilename(
            parent=self.root, title="导出 GIF", defaultextension=".gif",
            filetypes=[("GIF 动图", "*.gif")],
            initialdir=self.source_path.parent, initialfile=filename,
        )
        if not name:
            return
        destination = Path(name)
        self.export_button.state(["disabled"])
        self.status.configure(text="正在生成 GIF…")
        self.export_future = self.pool.submit(export_gif, self.source, settings, destination)
        self.root.after(50, self.check_export, destination)

    def check_export(self, destination):
        if not self.export_future.done():
            self.root.after(50, self.check_export, destination)
            return
        try:
            (width, height), cycle = self.export_future.result()
        except (OSError, ValueError) as error:
            self.status.configure(text="导出失败")
            messagebox.showerror("未能导出 GIF", str(error), parent=self.root)
        else:
            self.status.configure(text=f"已导出 {destination.name} · {width} × {height} · {cycle / 1000:g} 秒/轮")
        finally:
            self.export_future = None
            self.export_button.state(["!disabled"])

    def close(self):
        self.pool.shutdown(wait=False)
        self.root.destroy()


def main():
    root, drop_format, drop_status = create_window()
    try:
        settings = load_settings()
    except (OSError, ValueError) as error:
        root.withdraw()
        messagebox.showerror("无法读取配置", f"{CONFIG_PATH.name}\n{error}", parent=root)
        root.destroy()
        return
    app = BounceApp(root, settings, drop_format, drop_status)
    if len(sys.argv) > 1:
        root.after_idle(app.open_image, Path(sys.argv[1]))
    root.mainloop()


if __name__ == "__main__":
    main()
