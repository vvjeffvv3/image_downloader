Option Explicit

Dim shell, fso, folder, program, executable, prefix, bundled, command, arg, code
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
program = fso.BuildPath(folder, "launcher.pyw")
shell.CurrentDirectory = folder
shell.Environment("Process")("PYTHONUTF8") = "1"

' pythonw/pyw have no terminal window. The bundled runtime works on this PC.
bundled = shell.ExpandEnvironmentStrings("%USERPROFILE%") & _
    "\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe"
executable = ""
prefix = ""
If fso.FileExists(bundled) Then
    executable = bundled
Else
    executable = FindOnPath("pyw.exe")
    If executable <> "" Then
        prefix = " -3"
    Else
        executable = FindOnPath("pythonw.exe")
    End If
End If

If executable = "" Then
    MsgBox "Python 3.10 or newer is required. Install Python from https://www.python.org/downloads/", _
        vbExclamation, "YouTube and X Photo Downloader"
    WScript.Quit 1
End If

command = Quoted(executable) & prefix & " " & Quoted(program)
For Each arg In WScript.Arguments
    command = command & " " & Quoted(arg)
Next

On Error Resume Next
' Hide the launcher; the Python app creates its own normal GUI window.
code = shell.Run(command, 0, WScript.Arguments.Count > 0)
If Err.Number <> 0 Then
    MsgBox "Could not start the downloader: " & Err.Description, vbExclamation, "YouTube and X Photo Downloader"
    WScript.Quit 1
End If
On Error GoTo 0
WScript.Quit code

Function FindOnPath(name)
    Dim entry, candidate
    FindOnPath = ""
    For Each entry In Split(shell.ExpandEnvironmentStrings("%PATH%"), ";")
        entry = Replace(Trim(entry), Chr(34), "")
        If entry <> "" Then
            candidate = fso.BuildPath(entry, name)
            If fso.FileExists(candidate) Then
                FindOnPath = candidate
                Exit Function
            End If
        End If
    Next
End Function

Function Quoted(value)
    Quoted = Chr(34) & Replace(value, Chr(34), Chr(34) & Chr(34)) & Chr(34)
End Function
