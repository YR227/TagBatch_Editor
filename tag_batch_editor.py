#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
"""
曲タグ一括編集ツール (TagBatchEditor)

曲タイトル（またはファイル名）を「テンプレート」で分解して、
タイトル / アルバム / アーティスト / アルバムアーティスト / 作曲者 / ジャンル /
作詞者 / トラック番号 / ディスク番号 / 年 / コメント をまとめて書き換えます。

必要なもの:  Python 3.8+  と  mutagen   (pip install mutagen)
起動:        python tag_batch_editor.py

テンプレートの書き方:
  {名前}    … 任意の文字列を取り出す（名前は自由。例: {アーティスト} {曲名} {a}）
  {名前#}   … 数字だけを取り出す（トラック番号・ディスク番号向け）
  {*}       … その部分は読み飛ばす
  それ以外  … 区切り文字（「 - 」など）。前後の空白の有無は多少ゆるく判定します

  例: 「Supire - Howling」             → {アーティスト} - {タイトル}
  例: 「作曲者 - 1-03 - 曲名」         → {アーティスト} - {ディスク#}-{トラック#} - {タイトル}

各プロパティ欄の書き方:
  空欄        … 変更しない
  {名前}      … テンプレートで取り出した値を入れる（複数欄に同じものを入れてOK）
  普通の文字  … その文字を全曲に入れる（例: ジャンル欄に「Electronic」）
  組み合わせ  … 「{アーティスト} feat. X」のような書き方も可
  <削除>      … そのタグを消す
"""

import os
import re
import sys
import json
import shutil
import datetime

APP_NAME = "TagBatchEditor"
APP_VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# 項目定義
# ---------------------------------------------------------------------------
FIELDS = [
    ("title", "タイトル"),
    ("album", "アルバム"),
    ("artist", "アーティスト"),
    ("albumartist", "アルバムアーティスト"),
    ("composer", "作曲者"),
    ("genre", "ジャンル"),
    ("lyricist", "作詞者"),
    ("tracknumber", "トラック番号"),
    ("discnumber", "ディスク番号"),
    ("date", "年"),
    ("comment", "コメント"),
]
# 画面に出す項目（タグ11項目 + ファイル名）
UI_FIELDS = FIELDS + [("filename", "ファイル名")]
FIELD_LABEL = dict(UI_FIELDS)
LABEL_TO_KEY = {label: key for key, label in UI_FIELDS}

# Windowsでファイル名に使えない文字は全角に置き換える
_BAD_CHARS = str.maketrans({'\\': '＼', '/': '／', ':': '：', '*': '＊', '?': '？',
                            '"': '”', '<': '＜', '>': '＞', '|': '｜'})


def safe_filename(name):
    name = "".join(c for c in name.translate(_BAD_CHARS) if ord(c) >= 32)
    return name.strip().rstrip(". ")


def renamed_path(path, new_stem):
    ext = os.path.splitext(path)[1]
    return os.path.join(os.path.dirname(path), safe_filename(new_stem) + ext)


def stem_of(path):
    return os.path.splitext(os.path.basename(path))[0]
# 自動割当で使う別名
ALIASES = {
    "曲名": "title", "title": "title",
    "artist": "artist", "アルバムアーティスト": "albumartist",
    "作曲": "composer", "トラック": "tracknumber", "track": "tracknumber",
    "ディスク": "discnumber", "disc": "discnumber", "cd": "discnumber", "CD番号": "discnumber",
    "album": "album", "year": "date", "genre": "genre",
}
AUDIO_EXTS = (".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".opus", ".wav", ".aif", ".aiff")
DELETE_MARK = "<削除>"

PRESETS = [
    {
        "name": "シングル: アーティスト - 曲名",
        "source": "title",
        "template": "{アーティスト} - {タイトル}",
        "map": {"title": "{タイトル}", "artist": "{アーティスト}", "composer": "{アーティスト}"},
    },
    {
        "name": "アルバム: 作曲者 - CD番号-トラック番号 - 曲名",
        "source": "title",
        "template": "{アーティスト} - {ディスク#}-{トラック#} - {タイトル}",
        "map": {"title": "{タイトル}", "artist": "{アーティスト}", "composer": "{アーティスト}",
                "tracknumber": "{トラック}", "discnumber": "{ディスク}"},
    },
    {
        "name": "アルバム: アーティスト - トラック番号 - 曲名",
        "source": "title",
        "template": "{アーティスト} - {トラック#} - {タイトル}",
        "map": {"title": "{タイトル}", "artist": "{アーティスト}", "composer": "{アーティスト}",
                "tracknumber": "{トラック}"},
    },
    {
        "name": "Bandcampのファイル名: アーティスト - アルバム - 01 曲名",
        "source": "filename",
        "template": "{アーティスト} - {アルバム} - {トラック#} {タイトル}",
        "map": {"title": "{タイトル}", "album": "{アルバム}", "artist": "{アーティスト}",
                "albumartist": "{アーティスト}", "tracknumber": "{トラック}"},
    },
    {
        "name": "トラック番号 曲名",
        "source": "filename",
        "template": "{トラック#} {タイトル}",
        "map": {"title": "{タイトル}", "tracknumber": "{トラック}"},
    },
]

# ---------------------------------------------------------------------------
# テンプレート解析（GUI・mutagenに依存しない部分）
# ---------------------------------------------------------------------------
TOKEN_RE = re.compile(r"\{([^{}]*)\}")


def _literal_to_regex(s):
    """区切り文字部分を正規表現に。空白は数や有無をゆるく扱う。"""
    if s == "":
        return ""
    if s.isspace():
        return r"\s+"
    out = ""
    for chunk in re.split(r"(\s+)", s):
        if not chunk:
            continue
        # テンプレートに空白がある所は、空白が1つ以上必要（数は問わない）
        out += r"\s+" if chunk.isspace() else re.escape(chunk)
    return out


def compile_template(tpl):
    """テンプレートを (正規表現, 取り出す名前のリスト) に変換する。"""
    tpl = tpl.strip()
    regex = r"^\s*"
    names = []
    pos = 0
    for m in TOKEN_RE.finditer(tpl):
        regex += _literal_to_regex(tpl[pos:m.start()])
        pos = m.end()
        raw = m.group(1).strip()
        numeric = raw.endswith("#")
        name = raw[:-1].strip() if numeric else raw
        if name in ("", "*"):
            regex += r"(?:\d+)" if numeric else r"(?:.*?)"
            continue
        pat = r"\d+" if numeric else r".+?"
        if name in names:
            regex += "(?P=g%d)" % names.index(name)
        else:
            regex += "(?P<g%d>%s)" % (len(names), pat)
            names.append(name)
    regex += _literal_to_regex(tpl[pos:]) + r"\s*$"
    return re.compile(regex), names


def match_template(compiled, text):
    regex, names = compiled
    m = regex.match(text or "")
    if not m:
        return None
    return {name: m.group("g%d" % i).strip() for i, name in enumerate(names)}


def expand(expr, values):
    """プロパティ欄の式を展開。未知の {名前} はそのまま残す。"""
    def rep(m):
        key = m.group(1).strip().rstrip("#").strip()
        return values.get(key, m.group(0))
    return TOKEN_RE.sub(rep, expr)


def compute_changes(values, mapping, strip_zeros=True):
    """取り出した値とプロパティ割当から、書き込む内容 {key: 値 or None(削除)} を作る。"""
    changes = {}
    for key, expr in mapping.items():
        expr = expr.strip()
        if not expr:
            continue
        if expr == DELETE_MARK:
            if key != "filename":
                changes[key] = None
            continue
        v = expand(expr, values).strip()
        if key == "filename":
            v = safe_filename(v)
        if not v:
            continue
        if strip_zeros and key in ("tracknumber", "discnumber"):
            v = re.sub(r"\d+", lambda m: str(int(m.group(0))), v)
        changes[key] = v
    return changes


# ---------------------------------------------------------------------------
# タグの読み書き（mutagen）
# ---------------------------------------------------------------------------
try:
    import mutagen
    from mutagen.easyid3 import EasyID3
    from mutagen.easymp4 import EasyMP4Tags
    from mutagen.mp3 import EasyMP3
    from mutagen.id3 import COMM, Frames
    from mutagen.wave import WAVE
    from mutagen.aiff import AIFF
    HAVE_MUTAGEN = True
except ImportError:
    HAVE_MUTAGEN = False


def _setup_mutagen():
    # MP3: コメント(COMM)をEasyID3で扱えるようにする
    def comm_frames(id3):
        return [(k, f) for k, f in id3.items() if k.startswith("COMM") and getattr(f, "desc", None) == ""]

    def comm_get(id3, key):
        frames = comm_frames(id3)
        if not frames:
            raise KeyError(key)
        return [t for _, f in frames for t in f.text]

    def comm_delete(id3, key):
        for k, _ in comm_frames(id3):
            del id3[k]

    def comm_set(id3, key, value):
        comm_delete(id3, key)
        id3.add(COMM(encoding=3, lang="eng", desc="", text=list(value)))

    EasyID3.RegisterKey("comment", comm_get, comm_set, comm_delete)
    if "lyricist" not in EasyID3.valid_keys:
        EasyID3.RegisterTextKey("lyricist", "TEXT")
    # M4A: 作詞者は標準項目がないのでフリーフォームで
    try:
        EasyMP4Tags.RegisterFreeformKey("lyricist", "LYRICIST")
    except Exception:
        pass


if HAVE_MUTAGEN:
    _setup_mutagen()


# WAV / AIFF は ID3 タグを直接扱う
ID3_FRAME = {
    "title": "TIT2", "album": "TALB", "artist": "TPE1", "albumartist": "TPE2",
    "composer": "TCOM", "genre": "TCON", "lyricist": "TEXT", "tracknumber": "TRCK",
    "discnumber": "TPOS", "date": "TDRC",
}


def _is_raw_id3(audio):
    return isinstance(audio, (WAVE, AIFF))


def _raw_comm(tags):
    return [f for f in tags.getall("COMM") if f.desc == ""]


def _read_raw_id3(audio):
    tags = audio.tags
    out = {}
    for key, _ in FIELDS:
        if tags is None:
            out[key] = ""
            continue
        if key == "comment":
            frames = _raw_comm(tags)
        else:
            frames = tags.getall(ID3_FRAME[key])
            if key == "date" and not frames:
                frames = tags.getall("TYER")
        out[key] = "; ".join(str(t) for f in frames for t in f.text)
    return out


def _write_raw_id3(audio, changes):
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    for key, value in changes.items():
        if key == "comment":
            for f in _raw_comm(tags):
                tags.delall("COMM:%s:%s" % (f.desc, f.lang))
            if value:
                tags.add(COMM(encoding=3, lang="eng", desc="", text=[value]))
        else:
            fid = ID3_FRAME[key]
            tags.delall(fid)
            if key == "date":
                tags.delall("TYER")
            if value:
                tags.add(Frames[fid](encoding=3, text=[value]))
    audio.save(v2_version=3)


# ---------------------------------------------------------------------------
# WAV の RIFF INFO（Windowsのエクスプローラーが表示に使う方式）
# アルバムアーティスト・ディスク番号はこの方式に項目がないため書き込まれません
# ---------------------------------------------------------------------------
import struct

INFO_ID = {
    "title": b"INAM", "artist": b"IART", "album": b"IPRD", "genre": b"IGNR",
    "date": b"ICRD", "comment": b"ICMT", "tracknumber": b"ITRK",
    "composer": b"IMUS", "lyricist": b"IWRI",
}


def _riff_walk(data):
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("WAVの形式が想定外です")
    chunks, pos = [], 12
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        end = pos + 8 + size + (size & 1)
        if pos + 8 + size > len(data):
            raise ValueError("WAVの構造が想定外のためエクスプローラー用タグは書き込めません")
        chunks.append((cid, pos, size))
        pos = min(end, len(data))
    return chunks


def _is_info(data, cid, pos):
    return cid == b"LIST" and data[pos + 8:pos + 12] == b"INFO"


def _info_entries(data, pos, size):
    out, p, end = [], pos + 12, pos + 8 + size
    while p + 8 <= end:
        sid = data[p:p + 4]
        ssize = struct.unpack("<I", data[p + 4:p + 8])[0]
        out.append((sid, data[p + 8:p + 8 + ssize]))
        p += 8 + ssize + (ssize & 1)
    return out


def _decode_info(b):
    b = b.split(b"\x00", 1)[0]
    try:
        return b.decode("cp932")
    except UnicodeDecodeError:
        return b.decode("utf-8", errors="replace")


def _encode_info(s):
    """日本語Windowsのエクスプローラーは INFO を Shift_JIS(cp932) として読むので、
    cp932 にない文字（à など）は近い文字に置き換える（à→a, é→e）。"""
    import unicodedata
    out = []
    for ch in s:
        try:
            ch.encode("cp932")
            out.append(ch)
        except UnicodeEncodeError:
            base = "".join(c for c in unicodedata.normalize("NFKD", ch)
                           if not unicodedata.combining(c))
            try:
                base.encode("cp932")
                out.append(base if base else "?")
            except UnicodeEncodeError:
                out.append("?")
    return "".join(out).encode("cp932") + b"\x00"


def read_riff_info(path):
    with open(path, "rb") as f:
        data = f.read()
    out = {}
    try:
        for cid, pos, size in _riff_walk(data):
            if _is_info(data, cid, pos):
                rev = {v: k for k, v in INFO_ID.items()}
                for sid, val in _info_entries(data, pos, size):
                    if sid in rev:
                        out[rev[sid]] = _decode_info(val)
    except ValueError:
        pass
    return out


def write_riff_info(path, changes):
    with open(path, "rb") as f:
        data = f.read()
    chunks = _riff_walk(data)
    entries = []
    for cid, pos, size in chunks:
        if _is_info(data, cid, pos):
            entries.extend(_info_entries(data, pos, size))
    entries = dict(entries)
    for key, value in changes.items():
        sid = INFO_ID.get(key)
        if not sid:
            continue
        if value:
            entries[sid] = _encode_info(value)
        else:
            entries.pop(sid, None)
    body = b"INFO"
    for sid, val in entries.items():
        body += sid + struct.pack("<I", len(val)) + val + (b"\x00" if len(val) & 1 else b"")
    parts = [data[:12]]
    for cid, pos, size in chunks:
        if not _is_info(data, cid, pos):
            raw = data[pos:pos + 8 + size]
            parts.append(raw + (b"\x00" if size & 1 else b""))
    if len(entries):
        parts.append(b"LIST" + struct.pack("<I", len(body)) + body)
    out = b"".join(parts)
    out = out[:4] + struct.pack("<I", len(out) - 8) + out[8:]
    tmp = path + ".tagtmp"
    with open(tmp, "wb") as f:
        f.write(out)
    os.replace(tmp, path)


def read_tags(path):
    tags = _read_tags_core(path)
    tags["filename"] = stem_of(path)
    return tags


def _read_tags_core(path):
    audio = mutagen.File(path, easy=True)
    if audio is None:
        raise ValueError("未対応の形式です")
    if _is_raw_id3(audio):
        tags = _read_raw_id3(audio)
        if isinstance(audio, WAVE):
            for k, v in read_riff_info(path).items():
                if not tags.get(k):
                    tags[k] = v
        return tags
    tags = {}
    for key, _ in FIELDS:
        try:
            v = audio.get(key)
        except Exception:
            v = None
        tags[key] = "; ".join(str(x) for x in v) if v else ""
    return tags


def write_tags(path, changes):
    """changes: {key: 値}。値が None または空文字ならタグを削除。"""
    changes = {k: v for k, v in changes.items() if k != "filename"}
    if not changes:
        return
    audio = mutagen.File(path, easy=True)
    if audio is None:
        raise ValueError("未対応の形式です")
    if _is_raw_id3(audio):
        _write_raw_id3(audio, changes)
        if isinstance(audio, WAVE):
            write_riff_info(path, changes)
        return
    if audio.tags is None:
        audio.add_tags()
    for key, value in changes.items():
        if value is None or value == "":
            try:
                del audio[key]
            except KeyError:
                pass
        else:
            audio[key] = [value]
    if isinstance(audio, EasyMP3):
        audio.save(v2_version=3)  # Windowsのエクスプローラーで読みやすいID3v2.3で保存
    else:
        audio.save()


def apply_to_file(path, changes):
    """タグを書き込み、必要ならファイル名も変える。新しいパスを返す。"""
    write_tags(path, changes)
    new_stem = changes.get("filename")
    if new_stem:
        new_path = renamed_path(path, new_stem)
        if new_path != path:
            if os.path.exists(new_path) and os.path.normcase(new_path) != os.path.normcase(path):
                raise ValueError("同じ名前のファイルが既にあります: " + os.path.basename(new_path))
            os.rename(path, new_path)
            return new_path
    return path


def collect_files(folder, recursive):
    out = []
    if recursive:
        for root, _, files in os.walk(folder):
            for f in files:
                if f.lower().endswith(AUDIO_EXTS):
                    out.append(os.path.join(root, f))
    else:
        for f in os.listdir(folder):
            p = os.path.join(folder, f)
            if os.path.isfile(p) and f.lower().endswith(AUDIO_EXTS):
                out.append(p)
    return sorted(out)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, simpledialog
    import copy

    # 設定とバックアップはユーザーごとの設定フォルダに保存（exe化しても動くように）
    #   Windows: %APPDATA%\TagBatchEditor
    if os.name == "nt" and os.environ.get("APPDATA"):
        data_dir = os.path.join(os.environ["APPDATA"], APP_NAME)
    else:
        data_dir = os.path.join(os.path.expanduser("~"), "." + APP_NAME)
    os.makedirs(data_dir, exist_ok=True)
    backup_dir = os.path.join(data_dir, "tag_backups")
    settings_path = os.path.join(data_dir, "settings.json")

    # 以前の版（本体と同じフォルダに保存していた頃）の設定・バックアップを引き継ぐ
    if not getattr(sys, "frozen", False):
        old_dir = os.path.dirname(os.path.abspath(__file__))
        old_settings = os.path.join(old_dir, "tag_editor_settings.json")
        old_backups = os.path.join(old_dir, "tag_backups")
        try:
            if os.path.exists(old_settings) and not os.path.exists(settings_path):
                shutil.copy2(old_settings, settings_path)
            if os.path.isdir(old_backups):
                os.makedirs(backup_dir, exist_ok=True)
                for name in os.listdir(old_backups):
                    dst = os.path.join(backup_dir, name)
                    if not os.path.exists(dst):
                        shutil.copy2(os.path.join(old_backups, name), dst)
        except Exception:
            pass

    def load_settings():
        try:
            with open(settings_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    # Windowsの表示倍率に対応（ぼやけ防止）。Tk を作る前に呼ぶ必要がある
    if os.name == "nt":
        try:
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    class App:
        def __init__(self, root):
            self.root = root
            root.title("曲タグ一括編集ツール  v" + APP_VERSION)
            # 表示倍率（100%=1.0, 150%=1.5）
            self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
            root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
            if os.name == "nt":
                import tkinter.font as tkfont
                for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                    try:
                        tkfont.nametofont(name).configure(family="Yu Gothic UI", size=10)
                    except Exception:
                        pass
            style = ttk.Style(root)
            style.configure("Treeview", rowheight=int(24 * self.scale))

            self.settings = load_settings()
            self.col_widths = dict(self.settings.get("col_widths", {}))
            self.presets = self.settings.get("presets") or copy.deepcopy(PRESETS)
            root.geometry(self.settings.get("geometry") or "%dx%d" % (1250 * self.scale, 780 * self.scale))
            root.protocol("WM_DELETE_WINDOW", self.on_close)

            self.items = []          # [{path, tags, error}]
            self.results = {}        # path -> {"src", "values", "changes"}
            self.iid_to_path = {}
            self._after = None

            self.source = tk.StringVar(value="title")
            self.template = tk.StringVar()
            self.recursive = tk.BooleanVar(value=True)
            self.strip_zeros = tk.BooleanVar(value=True)
            self.map_vars = {key: tk.StringVar() for key, _ in UI_FIELDS}
            self.status = tk.StringVar(value="フォルダかファイルを追加してください")

            self._build()
            for v in [self.source, self.template, self.strip_zeros, *self.map_vars.values()]:
                v.trace_add("write", lambda *a: self.schedule_refresh())

        # ---- 画面構成 ----
        def _build(self):
            pad = {"padx": 6, "pady": 4}

            top = ttk.Frame(self.root)
            top.pack(fill="x", **pad)
            ttk.Button(top, text="フォルダを追加", command=self.add_folder).pack(side="left")
            ttk.Button(top, text="ファイルを追加", command=self.add_files).pack(side="left", padx=4)
            ttk.Checkbutton(top, text="サブフォルダも", variable=self.recursive).pack(side="left", padx=4)
            ttk.Button(top, text="一覧をクリア", command=self.clear).pack(side="left", padx=12)
            ttk.Button(top, text="選択した行を外す", command=self.remove_selected).pack(side="left")

            rule = ttk.LabelFrame(self.root, text="① 分解のルール")
            rule.pack(fill="x", **pad)
            r0 = ttk.Frame(rule)
            r0.pack(fill="x", **pad)
            ttk.Label(r0, text="読み取り元:").pack(side="left")
            ttk.Radiobutton(r0, text="今のタイトルタグ", value="title", variable=self.source).pack(side="left")
            ttk.Radiobutton(r0, text="ファイル名（拡張子なし）", value="filename", variable=self.source).pack(side="left", padx=6)
            ttk.Label(r0, text="   プリセット:").pack(side="left")
            self.preset_box = ttk.Combobox(r0, state="readonly", width=48)
            self.preset_box.pack(side="left")
            self.preset_box.bind("<<ComboboxSelected>>", self.apply_preset)
            ttk.Button(r0, text="今の設定をプリセットに保存", command=self.save_preset).pack(side="left", padx=(6, 0))
            ttk.Button(r0, text="選択中のプリセットを削除", command=self.delete_preset).pack(side="left", padx=4)
            ttk.Button(r0, text="初期プリセットを戻す", command=self.reset_presets).pack(side="left")
            self.update_preset_box()

            r1 = ttk.Frame(rule)
            r1.pack(fill="x", **pad)
            ttk.Label(r1, text="テンプレート:").pack(side="left")
            ent = ttk.Entry(r1, textvariable=self.template, font=("", 12))
            ent.pack(side="left", fill="x", expand=True, padx=4)
            ttk.Label(rule, foreground="#555",
                      text="{名前}=文字を取り出す　{名前#}=数字だけ取り出す　{*}=読み飛ばす　"
                           "それ以外=区切り文字　　例: {アーティスト} - {ディスク#}-{トラック#} - {タイトル}"
                      ).pack(anchor="w", padx=8, pady=(0, 4))

            mp = ttk.LabelFrame(self.root, text="② 各プロパティに入れる内容（空欄=変更しない／{名前}=取り出した値／普通の文字=全曲に固定値／<削除>=消す）")
            mp.pack(fill="x", **pad)
            grid = ttk.Frame(mp)
            grid.pack(fill="x", **pad)
            cols = 4
            for i, (key, label) in enumerate(UI_FIELDS):
                r, c = divmod(i, cols)
                ttk.Label(grid, text=label).grid(row=r, column=c * 2, sticky="e", padx=(8, 2), pady=2)
                ttk.Entry(grid, textvariable=self.map_vars[key], width=22).grid(
                    row=r, column=c * 2 + 1, sticky="we", pady=2)
            for c in range(cols):
                grid.columnconfigure(c * 2 + 1, weight=1)
            btns = ttk.Frame(mp)
            btns.pack(fill="x", **pad)
            ttk.Button(btns, text="テンプレートの名前から自動割当", command=self.auto_map).pack(side="left")
            ttk.Button(btns, text="割当を全部クリア", command=self.clear_map).pack(side="left", padx=4)
            ttk.Checkbutton(btns, text="トラック/ディスク番号の先頭の0を取る（03→3）",
                            variable=self.strip_zeros).pack(side="left", padx=12)

            pv = ttk.LabelFrame(self.root, text="③ プレビュー（赤い行はルールに合わないので書き換えません）")
            pv.pack(fill="both", expand=True, **pad)
            self.tree = ttk.Treeview(pv, show="headings", selectmode="extended")
            ys = ttk.Scrollbar(pv, orient="vertical", command=self.tree.yview)
            xs = ttk.Scrollbar(pv, orient="horizontal", command=self.tree.xview)
            self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
            self.tree.grid(row=0, column=0, sticky="nsew")
            ys.grid(row=0, column=1, sticky="ns")
            xs.grid(row=1, column=0, sticky="ew")
            pv.rowconfigure(0, weight=1)
            pv.columnconfigure(0, weight=1)
            self.tree.tag_configure("ng", foreground="#c0392b")
            self.tree.tag_configure("same", foreground="#888")
            self.tree.bind("<ButtonRelease-1>", lambda e: self.remember_widths())

            bottom = ttk.Frame(self.root)
            bottom.pack(fill="x", **pad)
            ttk.Label(bottom, textvariable=self.status).pack(side="left")
            ttk.Button(bottom, text="④ 適用（タグを書き込む）", command=self.apply).pack(side="right")
            ttk.Button(bottom, text="バックアップから元に戻す", command=self.restore).pack(side="right", padx=6)
            ttk.Button(bottom, text="設定・バックアップのフォルダを開く", command=self.open_data_dir).pack(side="right")

        # ---- ファイル管理 ----
        def _load(self, paths):
            known = {it["path"] for it in self.items}
            for p in paths:
                if p in known:
                    continue
                try:
                    self.items.append({"path": p, "tags": read_tags(p), "error": None})
                except Exception as e:
                    self.items.append({"path": p, "tags": dict({k: "" for k, _ in FIELDS}, filename=stem_of(p)), "error": str(e)})
            self.refresh()

        def add_folder(self):
            d = filedialog.askdirectory()
            if d:
                self._load(collect_files(d, self.recursive.get()))

        def add_files(self):
            fs = filedialog.askopenfilenames(filetypes=[("音楽ファイル", " ".join("*" + e for e in AUDIO_EXTS)),
                                                        ("すべて", "*.*")])
            if fs:
                self._load(list(fs))

        def clear(self):
            self.items = []
            self.refresh()

        def remove_selected(self):
            drop = {self.iid_to_path[i] for i in self.tree.selection()}
            self.items = [it for it in self.items if it["path"] not in drop]
            self.refresh()

        def _moved(self, old, new):
            for it in self.items:
                if it["path"] == old:
                    it["path"] = new

        def reload_tags(self):
            for it in self.items:
                try:
                    it["tags"] = read_tags(it["path"])
                    it["error"] = None
                except Exception as e:
                    it["error"] = str(e)

        # ---- ルール ----
        def apply_preset(self, _evt=None):
            i = self.preset_box.current()
            if i < 0:
                return
            p = self.presets[i]
            if "strip_zeros" in p:
                self.strip_zeros.set(p["strip_zeros"])
            self.source.set(p["source"])
            self.template.set(p["template"])
            for key, var in self.map_vars.items():
                var.set(p["map"].get(key, ""))

        def auto_map(self):
            try:
                _, names = compile_template(self.template.get())
            except re.error:
                return
            for name in names:
                key = LABEL_TO_KEY.get(name) or ALIASES.get(name) or ALIASES.get(name.lower())
                if key and not self.map_vars[key].get().strip():
                    self.map_vars[key].set("{%s}" % name)

        def clear_map(self):
            for v in self.map_vars.values():
                v.set("")

        def mapping(self):
            return {k: v.get() for k, v in self.map_vars.items()}

        # ---- 列幅の記憶 ----
        def remember_widths(self):
            for c in self.tree["columns"]:
                self.col_widths[c] = int(self.tree.column(c, "width"))

        def open_data_dir(self):
            if os.name == "nt":
                os.startfile(data_dir)
            else:
                messagebox.showinfo("保存場所", data_dir)

        # ---- プリセット編集 ----
        def update_preset_box(self, select=None):
            names = [p["name"] for p in self.presets]
            self.preset_box["values"] = names
            if select in names:
                self.preset_box.current(names.index(select))
            else:
                self.preset_box.set("")

        def save_preset(self):
            names = [p["name"] for p in self.presets]
            name = simpledialog.askstring(
                "プリセットに保存", "プリセットの名前（同じ名前なら上書き）:",
                initialvalue=self.preset_box.get() or self.template.get(), parent=self.root)
            if not name or not name.strip():
                return
            name = name.strip()
            p = {"name": name, "source": self.source.get(), "template": self.template.get(),
                 "map": {k: v for k, v in self.mapping().items() if v.strip()},
                 "strip_zeros": self.strip_zeros.get()}
            if name in names:
                if not messagebox.askyesno("上書き確認", "「%s」を今の設定で上書きしますか？" % name):
                    return
                self.presets[names.index(name)] = p
            else:
                self.presets.append(p)
            self.update_preset_box(select=name)
            self.save_settings()

        def delete_preset(self):
            i = self.preset_box.current()
            if i < 0:
                messagebox.showinfo("削除", "削除するプリセットを選んでください。")
                return
            name = self.presets[i]["name"]
            if not messagebox.askyesno("削除確認", "プリセット「%s」を削除しますか？" % name):
                return
            del self.presets[i]
            self.update_preset_box()
            self.save_settings()

        def reset_presets(self):
            names = [p["name"] for p in self.presets]
            missing = [p for p in PRESETS if p["name"] not in names]
            if not missing:
                messagebox.showinfo("初期プリセット", "初期プリセットは全部そろっています。")
                return
            self.presets.extend(copy.deepcopy(missing))
            self.update_preset_box()
            self.save_settings()
            messagebox.showinfo("初期プリセット", "%d件の初期プリセットを戻しました。" % len(missing))

        def save_settings(self):
            self.remember_widths()
            data = dict(self.settings, col_widths=self.col_widths, geometry=self.root.geometry(),
                        presets=self.presets)
            self.settings = data
            try:
                with open(settings_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
            except Exception as e:
                messagebox.showerror("保存失敗", "設定を保存できませんでした:\n%s" % e)

        def on_close(self):
            self.remember_widths()
            data = dict(self.settings, col_widths=self.col_widths, geometry=self.root.geometry(),
                        presets=self.presets)
            try:
                with open(settings_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
            except Exception:
                pass
            self.root.destroy()

        # ---- プレビュー ----
        def schedule_refresh(self):
            if self._after:
                self.root.after_cancel(self._after)
            self._after = self.root.after(200, self.refresh)

        def refresh(self):
            self._after = None
            mapping = self.mapping()
            shown = [k for k, _ in UI_FIELDS if mapping[k].strip()]
            cols = ["file", "src", "status"] + shown
            self.remember_widths()   # 列を作り直す前に、今の幅を覚える
            self.tree.delete(*self.tree.get_children())
            self.tree["columns"] = cols
            heads = {"file": "ファイル", "src": "読み取った文字列", "status": "判定"}
            widths = {"file": 220, "src": 280, "status": 60}
            for c in cols:
                self.tree.heading(c, text=heads.get(c, FIELD_LABEL.get(c, c)))
                w = self.col_widths.get(c) or int(widths.get(c, 140) * self.scale)
                self.tree.column(c, width=w, stretch=False)

            try:
                compiled = compile_template(self.template.get()) if self.template.get().strip() else None
                tpl_error = None
            except re.error as e:
                compiled, tpl_error = None, str(e)

            self.results = {}
            self.iid_to_path = {}
            ok_count = 0
            rows = []
            for it in self.items:
                path = it["path"]
                if self.source.get() == "filename":
                    src = stem_of(path)
                else:
                    src = it["tags"].get("title", "")
                values = match_template(compiled, src) if compiled else None
                if it["error"]:
                    status, changes, tag = "読込失敗", {}, "ng"
                elif compiled is None:
                    status, tag = "-", ""
                    changes = compute_changes({}, mapping, self.strip_zeros.get())
                elif values is None:
                    status, changes, tag = "✗", {}, "ng"
                else:
                    changes = compute_changes(values, mapping, self.strip_zeros.get())
                    same = all((it["tags"].get(k, "") == (v or "")) for k, v in changes.items())
                    status, tag = ("変化なし", "same") if same else ("○", "")
                rows.append([it, src, values, changes, status, tag])

            # ファイル名の重複チェック
            new_names = {}
            for r in rows:
                if r[5] == "" and r[3].get("filename"):
                    np_ = os.path.normcase(renamed_path(r[0]["path"], r[3]["filename"]))
                    new_names.setdefault(np_, []).append(r)
            for np_, group in new_names.items():
                for r in group:
                    own = os.path.normcase(r[0]["path"])
                    if len(group) > 1 or (np_ != own and os.path.exists(np_)):
                        r[4], r[5] = "名前重複", "ng"

            for it, src, values, changes, status, tag in rows:
                path = it["path"]
                ok = tag == "" and bool(changes)
                if ok:
                    ok_count += 1
                self.results[path] = {"src": src, "values": values, "changes": changes, "ok": ok}
                row = [os.path.basename(path), src, status]
                for k in shown:
                    if k in changes:
                        row.append("（削除）" if changes[k] is None else changes[k])
                    else:
                        row.append("")
                iid = self.tree.insert("", "end", values=row, tags=(tag,) if tag else ())
                self.iid_to_path[iid] = path

            msg = "%d曲中 %d曲を書き換え予定" % (len(self.items), ok_count)
            if self.items and self.source.get() == "title" and all(not r["src"] for r in self.results.values()):
                msg += "　※タイトルタグが空です。読み取り元を「ファイル名」にしてみてください"
            if tpl_error:
                msg += "　※テンプレートの書き方に誤りがあります: " + tpl_error
            self.status.set(msg)

        # ---- 書き込み ----
        def apply(self):
            targets = [(p, r["changes"]) for p, r in self.results.items() if r["ok"]]
            if not targets:
                messagebox.showinfo("適用", "書き換える曲がありません。")
                return
            if not messagebox.askyesno("確認", "%d曲のタグ（とファイル名）を書き換えます。\n（変更前の内容はバックアップとして保存します）\n\nよろしいですか？" % len(targets)):
                return
            tags_by_path = {it["path"]: it["tags"] for it in self.items}
            # バックアップは「書き換え後のパス」をキーに保存（ファイル名を変えても戻せるように）
            backup = {}
            for p, ch in targets:
                final = renamed_path(p, ch["filename"]) if ch.get("filename") else p
                backup[final] = {k: tags_by_path[p].get(k, "") for k in ch}
            os.makedirs(backup_dir, exist_ok=True)
            bpath = os.path.join(backup_dir, datetime.datetime.now().strftime("backup_%Y%m%d_%H%M%S.json"))
            with open(bpath, "w", encoding="utf-8") as f:
                json.dump(backup, f, ensure_ascii=False, indent=1)

            errors = []
            for p, ch in targets:
                try:
                    self._moved(p, apply_to_file(p, ch))
                except Exception as e:
                    errors.append("%s: %s" % (os.path.basename(p), e))
            self.reload_tags()
            self.refresh()
            done = len(targets) - len(errors)
            text = "%d曲を書き換えました。\nバックアップ: %s" % (done, bpath)
            if errors:
                text += "\n\n失敗:\n" + "\n".join(errors[:20])
            messagebox.showinfo("完了", text)

        def restore(self):
            f = filedialog.askopenfilename(initialdir=backup_dir if os.path.isdir(backup_dir) else data_dir,
                                           filetypes=[("バックアップ", "*.json")])
            if not f:
                return
            with open(f, encoding="utf-8") as fp:
                data = json.load(fp)
            if not messagebox.askyesno("確認", "%d曲を、このバックアップの時点の内容に戻します。よろしいですか？" % len(data)):
                return
            errors = []
            for p, tags in data.items():
                try:
                    self._moved(p, apply_to_file(p, tags))
                except Exception as e:
                    errors.append("%s: %s" % (os.path.basename(p), e))
            self.reload_tags()
            self.refresh()
            messagebox.showinfo("完了", "元に戻しました。" + ("\n失敗:\n" + "\n".join(errors[:20]) if errors else ""))

    root = tk.Tk()
    if not HAVE_MUTAGEN:
        root.withdraw()
        messagebox.showerror("mutagen が必要です",
                             "タグの読み書きに mutagen を使います。\nコマンドプロンプトで\n\n  pip install mutagen\n\nを実行してから起動し直してください。")
        return
    App(root)
    root.mainloop()


if __name__ == "__main__":
    run_gui()
