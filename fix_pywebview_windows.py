"""One-time fix-up for two real bugs in pywebview 6.2.1's Windows backend,
run once after `pip install -r requirements.txt` and before building (or
running from source) on Windows. Both were found and fixed by actually
testing on a real machine -- see BUILD_WINDOWS.md for the full story of
each. Safe to re-run; both patches are idempotent.

1. Outdated bundled WebView2 assembly. pywebview ships
   Microsoft.Web.WebView2.Core.dll/.WinForms.dll built for classic .NET
   Framework (net462), which references a WinForms type
   (System.Windows.Forms.ContextMenu) that .NET's from-scratch WinForms
   port for .NET Core removed entirely -- loading it under modern .NET
   (which this app's launcher.py explicitly requests, see
   _init_dotnet_runtime()) throws a TypeLoadException. Fix: swap in the
   modern (.NET Core-targeted) build of those two assemblies from
   Microsoft's own WebView2 SDK NuGet package -- functionally identical
   API, just built against the right framework.

2. OpenFolderDialog class-body crash. webview/platforms/winforms.py has a
   class (unused by this app -- we have our own Tkinter folder picker)
   that reflects into a private, .NET-Framework-only WinForms
   implementation detail (System.Windows.Forms.FileDialogNative+IFileDialog)
   at class-definition time, unconditionally, the moment the module is
   imported. That type doesn't exist under modern .NET's WinForms port,
   so the lookup returns None, and the next line's `.GetMethod(...)`
   call on that None blows up importing the whole module -- taking down
   pywebview's native window with it (falls back to a browser tab
   instead, silently). Fix: guard those class-body assignments so the
   module can still import when the type isn't there; this specific
   dialog just becomes unavailable (harmless, since nothing here calls it).
"""
import io
import os
import sys
import urllib.request
import zipfile

NUGET_INDEX_URL = "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/index.json"
NUGET_PACKAGE_URL = "https://www.nuget.org/api/v2/package/Microsoft.Web.WebView2/{version}"


def _webview_lib_dir():
    import webview
    return os.path.join(os.path.dirname(webview.__file__), "lib")


def fix_webview2_assemblies():
    lib_dir = _webview_lib_dir()
    core_dll = os.path.join(lib_dir, "Microsoft.Web.WebView2.Core.dll")
    winforms_dll = os.path.join(lib_dir, "Microsoft.Web.WebView2.WinForms.dll")

    marker = os.path.join(lib_dir, ".notorious_bpm_patched_netcore")
    if os.path.isfile(marker):
        print("WebView2 assemblies: already patched, skipping.")
        return

    print("Fetching latest Microsoft.Web.WebView2 SDK version...")
    with urllib.request.urlopen(NUGET_INDEX_URL, timeout=15) as resp:
        import json
        versions = json.loads(resp.read())["versions"]
    latest = versions[-1]
    print(f"Downloading Microsoft.Web.WebView2 {latest}...")
    with urllib.request.urlopen(NUGET_PACKAGE_URL.format(version=latest), timeout=60) as resp:
        pkg_bytes = resp.read()

    with zipfile.ZipFile(io.BytesIO(pkg_bytes)) as z:
        src_prefix = "lib_manual/netcoreapp3.0/"
        for name in ("Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll"):
            with z.open(src_prefix + name) as src_f:
                data = src_f.read()
            dest = os.path.join(lib_dir, name)
            backup = dest + ".orig_net462"
            if not os.path.isfile(backup) and os.path.isfile(dest):
                os.rename(dest, backup)
            with open(dest, "wb") as dest_f:
                dest_f.write(data)
            print(f"  replaced {name}")

    with open(marker, "w") as f:
        f.write(f"patched with WebView2 SDK {latest}\n")
    print("WebView2 assemblies: patched.")


def fix_openfolderdialog():
    path = os.path.join(os.path.dirname(_webview_lib_dir()), "platforms", "winforms.py")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    if "Modern .NET's from-scratch WinForms port removed" in content:
        print("OpenFolderDialog guard: already patched, skipping.")
        return

    old = """class OpenFolderDialog:
    foldersFilter = 'Folders|\\n'
    flags = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic
    windowsFormsAssembly = Assembly.LoadWithPartialName('System.Windows.Forms')
    iFileDialogType = windowsFormsAssembly.GetType(
        'System.Windows.Forms.FileDialogNative+IFileDialog'
    )
    OpenFileDialogType = windowsFormsAssembly.GetType('System.Windows.Forms.OpenFileDialog')
    FileDialogType = windowsFormsAssembly.GetType('System.Windows.Forms.FileDialog')
    createVistaDialogMethodInfo = OpenFileDialogType.GetMethod('CreateVistaDialog', flags)
    onBeforeVistaDialogMethodInfo = OpenFileDialogType.GetMethod('OnBeforeVistaDialog', flags)
    getOptionsMethodInfo = FileDialogType.GetMethod('GetOptions', flags)
    setOptionsMethodInfo = iFileDialogType.GetMethod('SetOptions', flags)
    fosPickFoldersBitFlag = (
        windowsFormsAssembly.GetType('System.Windows.Forms.FileDialogNative+FOS')
        .GetField('FOS_PICKFOLDERS')
        .GetValue(None)
    )

    vistaDialogEventsConstructorInfo = windowsFormsAssembly.GetType(
        'System.Windows.Forms.FileDialog+VistaDialogEvents'
    ).GetConstructor(flags, None, [FileDialogType], [])
    adviseMethodInfo = iFileDialogType.GetMethod('Advise')
    unadviseMethodInfo = iFileDialogType.GetMethod('Unadvise')
    showMethodInfo = iFileDialogType.GetMethod('Show')

    @classmethod
    def show(cls, parent=None, initialDirectory=None, allow_multiple=False, title=None):"""

    new = """class OpenFolderDialog:
    foldersFilter = 'Folders|\\n'
    flags = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic
    windowsFormsAssembly = Assembly.LoadWithPartialName('System.Windows.Forms')
    iFileDialogType = windowsFormsAssembly.GetType(
        'System.Windows.Forms.FileDialogNative+IFileDialog'
    )
    OpenFileDialogType = windowsFormsAssembly.GetType('System.Windows.Forms.OpenFileDialog')
    FileDialogType = windowsFormsAssembly.GetType('System.Windows.Forms.FileDialog')
    # Modern .NET's from-scratch WinForms port removed the private
    # FileDialogNative implementation this reflection trick depends on --
    # it only ever existed under classic .NET Framework. There's no
    # drop-in replacement, so guard against it being gone instead of
    # crashing this whole module's import (patched in by Notorious B.P.M.'s
    # build process; harmless since nothing here calls OpenFolderDialog.show()).
    if iFileDialogType is not None:
        createVistaDialogMethodInfo = OpenFileDialogType.GetMethod('CreateVistaDialog', flags)
        onBeforeVistaDialogMethodInfo = OpenFileDialogType.GetMethod('OnBeforeVistaDialog', flags)
        getOptionsMethodInfo = FileDialogType.GetMethod('GetOptions', flags)
        setOptionsMethodInfo = iFileDialogType.GetMethod('SetOptions', flags)
        fosPickFoldersBitFlag = (
            windowsFormsAssembly.GetType('System.Windows.Forms.FileDialogNative+FOS')
            .GetField('FOS_PICKFOLDERS')
            .GetValue(None)
        )

        vistaDialogEventsConstructorInfo = windowsFormsAssembly.GetType(
            'System.Windows.Forms.FileDialog+VistaDialogEvents'
        ).GetConstructor(flags, None, [FileDialogType], [])
        adviseMethodInfo = iFileDialogType.GetMethod('Advise')
        unadviseMethodInfo = iFileDialogType.GetMethod('Unadvise')
        showMethodInfo = iFileDialogType.GetMethod('Show')
    else:
        createVistaDialogMethodInfo = None
        onBeforeVistaDialogMethodInfo = None
        getOptionsMethodInfo = None
        setOptionsMethodInfo = None
        fosPickFoldersBitFlag = None
        vistaDialogEventsConstructorInfo = None
        adviseMethodInfo = None
        unadviseMethodInfo = None
        showMethodInfo = None

    @classmethod
    def show(cls, parent=None, initialDirectory=None, allow_multiple=False, title=None):
        if cls.iFileDialogType is None:
            raise RuntimeError(
                'OpenFolderDialog is unavailable under modern .NET (WindowsDesktop) -- '
                'this pywebview feature relies on classic .NET Framework internals that '
                'were removed in the .NET Core WinForms port.'
            )"""

    if old not in content:
        print("OpenFolderDialog guard: expected pattern not found (pywebview version changed?) "
              "-- skipping this patch, check manually if the native window falls back to a "
              "browser tab.")
        return

    content = content.replace(old, new)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print("OpenFolderDialog guard: patched.")


if __name__ == "__main__":
    if sys.platform != "win32":
        print("This fix-up is Windows-only; nothing to do here.")
        sys.exit(0)
    fix_webview2_assemblies()
    fix_openfolderdialog()
    print("Done.")
