import sys, os, subprocess, tempfile, re, shutil, uuid, glob, distutils.spawn, urllib
from anki import media
from aqt import gui_hooks, mw, utils, operations, editor
from aqt.qt import QApplication, QClipboard, QAction, QKeySequence, QDialog, QShortcut, Qt, QDialogButtonBox

if utils.qtmajor == 6:
    from . import dialog_qt6 as dialog
elif utils.qtmajor == 5:
    from . import dialog_qt5 as dialog

if distutils.spawn.find_executable('ffmpeg'):
    FFMPEG = 'ffmpeg'
else:
    if sys.platform == 'win32' or sys.platform == 'cygwin':
        FFMPEG = os.path.join(os.path.dirname(__file__), 'ffmpeg', 'ffmpeg.exe')
    elif sys.platform == 'darwin':  
        FFMPEG = os.path.join(os.path.dirname(__file__), 'ffmpeg', 'ffmpeg')
    elif sys.platform == 'linux':
        utils.showWarning(text=f"""<p>Inline media depends on ffmpeg (https://ffmpeg.org/) for media conversion and was unable to find it in the system path. Please install ffmpeg through "sudo apt install ffmpeg" or similar.</p>""", parent=mw, title="Inline media", textFormat="rich")

CFG = {} # Default config
AUDIO = 'Audio format'
VIDEO = 'Video format'
AUTO_FRONT = 'Autoplay (front)'
AUTO_BACK = 'Autoplay (back)'
LOOP = 'Loop'
MUTE = 'Mute'
HEIGHT = 'Height'
WIDTH = 'Width'
MEDIA_TYPE = 'Media type'
AUDIO_SC = 'Audio shortcut'
VIDEO_SC = 'Video shortcut'
MEDIA_ID = r"im-media-[0-9a-z]{8}-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{12}"

###########################################################################
# Attribute dialog
###########################################################################
class IM_dialog(QDialog):
    ###########################################################################
    # Constructor (populates and shows dialog)
    ###########################################################################
    def __init__(self, editor, element, attribs):
        QDialog.__init__(self, editor)
        self.ui = dialog.Ui_dialog()
        self.ui.setupUi(self)
        self.ui.height.setSuffix(" px")
        self.ui.width.setSuffix(" px")
        self.ui.btn_del.clicked.connect(self.delete)
        QShortcut(QKeySequence(Qt.Modifier.CTRL |  Qt.Key.Key_Return), self).activated.connect(self.accept)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self).activated.connect(self.reject)
        QShortcut(QKeySequence(Qt.Modifier.CTRL | Qt.Key.Key_Delete), self).activated.connect(self.delete)
        self.element = element
        self.element_attribs = attribs
        if attribs.get(AUTO_FRONT):
            self.ui.auto_front.setChecked(True)
        if attribs.get(AUTO_BACK):
            self.ui.auto_back.setChecked(True)
        if attribs.get(LOOP):
            self.ui.loop.setChecked(True)
        if attribs.get(MUTE):
            self.ui.mute.setChecked(True)
        if attribs.get(MEDIA_TYPE, AUDIO) == VIDEO:
            self.ui.width.setValue(attribs.get(WIDTH, -1))
            self.ui.height.setValue(attribs.get(HEIGHT, -1))
        else:
            self.ui.width.setVisible(False)
            self.ui.width_lbl.setVisible(False)
            self.ui.height.setVisible(False)
            self.ui.height_lbl.setVisible(False)


    ###########################################################################
    # Attribute dialog accept
    ###########################################################################
    def accept(self):
        height = ''
        width = ''
        if self.element_attribs[MEDIA_TYPE] == VIDEO:
            if self.ui.height.value() == -1:
                height = "el.removeAttribute('height');"
            else:
                height = f"el.setAttribute('height', {str(self.ui.height.value())});"
            if self.ui.width.value() == -1:
                width = "el.removeAttribute('width');"
            else:
                width = f"el.setAttribute('width', {str(self.ui.width.value())});"
        self.parentWidget().eval(rf"""(function () {{
            let el = null;
            for (container of document.querySelectorAll('div.rich-text-editable')) {{
                if(el = container.shadowRoot.getElementById('{self.element}')) {{
                    break;
                }} 
            }}
            {"el.setAttribute('auto_front', true)" if self.ui.auto_front.isChecked() else "el.removeAttribute('auto_front')"};
            {"el.setAttribute('auto_back', true)" if self.ui.auto_back.isChecked() else "el.removeAttribute('auto_back')"};
            {"el.setAttribute('loop', true)" if self.ui.loop.isChecked() else "el.removeAttribute('loop')"};
            {"el.setAttribute('mute', true)" if self.ui.mute.isChecked() else "el.removeAttribute('mute')"};
            {height}
            {width}
        }})();""")
        return super().accept()

    ###########################################################################
    # Attribute dialog reject
    ###########################################################################
    def reject(self):
        QDialog.reject(self) 

    ###########################################################################
    # Attribute dialog delete
    ###########################################################################
    def delete(self):
        self.parentWidget().eval(rf"""(function () {{
            let el = null;
            for (container of document.querySelectorAll('div.rich-text-editable')) {{
                if(el = container.shadowRoot.getElementById('{self.element}')) {{
                    break;
                }} 
            }}
            el.remove();
        }})();""")
        return super().accept()


###########################################################################
# Parse clipboard
###########################################################################
def parse_clipboard():
    files = []
    for url in QApplication.clipboard().mimeData(QClipboard.Mode.Clipboard).urls():
        if url.isLocalFile():
            files.append({'url': None, 'path': url.toLocalFile()})
        else:
            files.append({'url': url.toString(), 'path': None})

    if not files and QApplication.clipboard().mimeData(QClipboard.Mode.Clipboard).hasText():
        url_re = re.compile(
            r'^(?:http|ftp)s?://' # http:// or https://
            r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|' #domain...
            r'localhost|' #localhost...
            r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})' # ...or ip
            r'(?::\d+)?' # optional port
            r'(?:/?|[/?]\S+|)$', # optional query parameter
            re.IGNORECASE)

        for line in QApplication.clipboard().mimeData(QClipboard.Mode.Clipboard).text().split('\n'):
            if url_re.match(line):
                files.append({'url': line, 'path': None})
            elif os.path.exists(line):
                files.append({'url': None, 'path': line})
    return files


###########################################################################
# Convert file in clipboard, add to media and insert in field
###########################################################################
def insert(weditor, fmt, files, opts):
    # Run file download and conversion in background
    def _convert(weditor, pos, fmt, files, opts):
        global media_attribs
        tmp_dir = tempfile.TemporaryDirectory()
        wins = []
        fails = []
        for src in files:
            if(src['url'] and not src['path']):
                src['path'] = os.path.join(tmp_dir.name, urllib.parse.unquote(src['url'].rsplit('/', 1)[-1]))
                remote = urllib.request.urlopen(src['url'])
                with open(src['path'], 'b+w') as fw:
                    fw.write(remote.read())
            
            (stem, ext) = os.path.splitext(src['path'])
            _dir = os.path.dirname(stem)
            _id =  f'im-media-{uuid.uuid4()}'
            if len(ext):
                ext = (ext[1:] if ext[0] == os.extsep else ext).lower()

            if (fmt == "audio" and ext == CFG[AUDIO]) or (fmt == "video" and ext == CFG[VIDEO]):
                dest = os.path.join(_dir, f'_{_id}.{ext}')
                shutil.copy(src['path'], dest)
            else:
                if fmt == "audio":
                    dest = os.path.join(tmp_dir.name, f'_{_id}.{CFG[AUDIO]}')
                    cmd = [FFMPEG, "-i", src['path'], "-vn", dest]
                else:
                    dest = os.path.join(tmp_dir.name, f'_{_id}.{CFG[VIDEO]}')
                    cmd = [FFMPEG, "-i", src['path'], dest]
                proc_info = subprocess.run(cmd, stdout=subprocess.PIPE, universal_newlines=True, shell=True)
            
            if os.path.exists(dest):
                file = mw.col.media.add_file(dest)
                os.remove(dest)
                dims =  ''
                if fmt == 'video':
                    if CFG[HEIGHT] > -1:
                        dims += ' height="{CFG[HEIGHT]}"'
                    if CFG[WIDTH] > -1:
                        dims += ' width="{CFG[WIDTH]}"'

                wins.append(''.join([
                    f'<{fmt} id="{_id}" class="inline-media"',
                    f' src="{file}" controls {media_attribs}{dims}',
                    " oncanplay=\"if(this.getRootNode().querySelector('anki-editable') === null",
                    " && this.offsetParent !== null",
                    " && ((this.hasAttribute('auto_front') && !document.body.classList.contains('back'))",
                    " || (this.hasAttribute('auto_back') && document.body.classList.contains('back'))))",
                    ' {this.play();}"',
                    " oncontextmenu=\"pycmd(this.id); return true;\""
                    f'></{fmt}>'
                ]))
            else:
                fails.append(src['path'] if src['path'] else src['url'])
                
            return (weditor, pos, wins, fails)

    # Create HTML element and insert
    def _insert(params):
        (weditor, pos, wins, fails) = params
        if len(wins):
            html = '&nbsp;' + '&nbsp;&nbsp;'.join(wins) + '&nbsp;'
            weditor.eval(f"""(function () {{
                const pos = document.activeElement.shadowRoot.getElementById('{pos}');
                pos.insertAdjacentHTML('beforebegin', `{html}`);
                pos.remove();
                return true;
            }})()""")

        if len(fails):
            weditor.eval(f"""(function () {{
                [...document.activeElement.shadowRoot.querySelectorAll("div[id^='im-tmp-]'")].forEach(el => {{
                    el.parentNode.removeChild(el);
                }});
            }})()""")
            utils.tooltip(f'Failed to insert {", ".join(fails)}.')


    # Convoluted solution with placeholder div to "store" current position
    pos = f'im-tmp-{uuid.uuid4()}'
    weditor.eval(f"""(function () {{
        const sel = document.activeElement.shadowRoot.getSelection();
        sel.collapseToEnd();
        const rng = sel.getRangeAt(0);
        const el = document.createElement('div');
        el.id = '{pos}'
        rng.insertNode(el);
        return true;
    }})();""")
    op = operations.QueryOp(parent=weditor, op=lambda col: _convert(weditor, pos, fmt, files, opts), success=_insert)
    op.with_progress().run_in_background()


###########################################################################
# Add shortcuts
###########################################################################
def register_shortcuts(scuts, editor):
    scuts.append([CFG[AUDIO_SC], lambda files=parse_clipboard(): insert(editor.web, "audio", files, CFG)])
    scuts.append([CFG[VIDEO_SC], lambda files=parse_clipboard(): insert(editor.web, "video", files, CFG)])


###########################################################################
# Context menu activation - build and append IM menu items
###########################################################################
def mouse_context(weditor, menu):
    global current_element
    files = parse_clipboard()
    if len(files) or current_element:
        menu.addSeparator()
        if len(files):
            audio = QAction("Insert clipboard as audio", menu)
            audio.triggered.connect(lambda: insert(weditor, "audio", files, CFG))
            audio.setShortcut(CFG[AUDIO_SC])
            menu.addAction(audio)
            video = QAction("Insert clipboard as video", menu)
            video.triggered.connect(lambda: insert(weditor, "video", files, CFG))
            video.setShortcut(CFG[VIDEO_SC])
            menu.addAction(video)
    
        if current_element:
            el = current_element
            current_element = ''
            ed = QAction("Edit media element", menu)
            ed.triggered.connect(lambda: edit_element(weditor, el))
            menu.addAction(ed)
        menu.addSeparator()

    return menu

def on_js_msg(handled, msg, context) -> tuple:
    global current_element
    if isinstance(context, editor.Editor) and re.match(rf'^{MEDIA_ID}$', msg):
        current_element = msg
        return (True, None)
    return handled

def edit_element(weditor, element):
    def _edit(attribs):
        if attribs == None:
            print("Inline media: Unable to find active element in document.")
            return
        IM_dialog(weditor, element, {
            MEDIA_TYPE: VIDEO if attribs[0] == 'VIDEO' else AUDIO,
            AUTO_FRONT: attribs[1],
            AUTO_BACK: attribs[2],
            LOOP: attribs[3],
            MUTE: attribs[4],
            HEIGHT: int(attribs[5]) if attribs[5] != None else -1,
            WIDTH: int(attribs[6]) if attribs[6] != None else -1
        }).exec()

    weditor.evalWithCallback(rf"""(function () {{
        let el = null;
        for (container of document.querySelectorAll('div.rich-text-editable')) {{
            if(el = container.shadowRoot.getElementById('{element}')) {{
                break;
            }} 
        }}
        return [el.tagName, el.getAttribute('auto_front'), el.getAttribute('auto_back'), el.getAttribute('loop'), el.getAttribute('mute'), el.getAttribute('height'), el.getAttribute('width')];
    }})();""", _edit)



###########################################################################
# Inline media check - verify which inline media files are no longer needed
###########################################################################
def media_check():
    # Check media in background
    def _check(col):
        notes = col.find_notes(r'"<audio *src=\"_im-media-" OR "<video *src=\"_im-media-"', False)
        refs = {}
        orphans = []
        for _id in notes:
            note = mw.col.get_note(_id)
            for fld in note.values():
                for match in re.finditer(rf'src="(_{MEDIA_ID}\.[0-9a-z]*)"', fld):
                    refs[match.group(1)] = True
        
        base_path = media.media_paths_from_col_path(col.path)[0]
        for path in glob.glob(os.path.join(base_path, '_im-media-*')):
            file = os.path.split(path)[-1]
            if re.match(rf'^_{MEDIA_ID}\.[0-9a-z]*$', file) and not refs.get(file):
                orphans.append(file)
        
        return orphans
    
    # Setup delete dialog and run delete in background
    def _check_complete(orphans):
        if len(orphans):
            # Launch delete in background on acccept
            def _accepted():
                dlg.accept()
                op = operations.QueryOp(parent=mw, op=lambda col: _delete(col, orphans), success=_delete_complete)
                op.with_progress().run_in_background()

            (dlg, btns) = utils.showText('Found the following orphaned files:\n' + '\n'.join(orphans) + '\n\nDelete these files?', title="Check Inline Media", run=False)
            btns.removeButton(btns.buttons()[0])
            btns.addButton("Delete", QDialogButtonBox.ButtonRole.AcceptRole)
            btn = btns.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
            btn.setDefault(True)
            btns.accepted.connect(_accepted)
            dlg.exec()
        
        else:
            utils.tooltip("No orphaned inline media files found.")

    # Run delete in background
    def _delete(col, orphans):
        base_path = media.media_paths_from_col_path(col.path)[0]
        cnt = 0
        for file in orphans:
            shutil.move(os.path.join(base_path, file), os.path.join(base_path, '..', 'media.trash', file))
            cnt += 1
        return cnt
    
    # cnt files deleted
    def _delete_complete(cnt):
        utils.tooltip(f'Deleted {cnt} orphaned inline media files.')


    op = operations.QueryOp(parent=mw, op=lambda col: _check(col), success=_check_complete)
    op.with_progress().run_in_background()

###########################################################################
# "Main" - load config and set up hooks
###########################################################################
current_element = ''
CFG = mw.addonManager.getConfig(__name__)
if not CFG.get(AUDIO):
    CFG[AUDIO] = 'ogg'
if not CFG.get(VIDEO):
    CFG[VIDEO] = 'webm'
CFG[AUDIO_SC] = CFG.get(AUDIO_SC, 0)
CFG[VIDEO_SC] = CFG.get(VIDEO_SC, 0)

media_attribs = '' # Default media attributes
if CFG.get(AUTO_FRONT):
    media_attribs += ' auto_front="true"'
if CFG.get(AUTO_BACK):
    media_attribs += ' auto_back="true"'
if CFG.get(LOOP):
    media_attribs += ' loop="true"'
if CFG.get(MUTE):
    media_attribs += ' mute="true"'
if not CFG.get(HEIGHT):
    CFG[HEIGHT] = -1
if not CFG.get(WIDTH):
    CFG[WIDTH] = -1

action = QAction('Check Inline Media', mw)
action.setToolTip('Check for orphaned inline media files (not detected by "Check Media")')
action.triggered.connect(media_check)
mw.form.menuTools.addAction(action)

gui_hooks.editor_did_init_shortcuts.append(register_shortcuts)
gui_hooks.editor_will_show_context_menu.append(mouse_context)
gui_hooks.webview_did_receive_js_message.append(on_js_msg)
