# Collect OutputDebugString output (the DBWIN protocol) into a file.
# Only one listener may run at a time; a debugger attached to a process
# receives that process's strings instead.
param([string]$Out = "C:\utmbot\ods.log", [int]$Seconds = 1800)
Add-Type @"
using System;
using System.IO;
using System.Threading;
using System.IO.MemoryMappedFiles;
public static class DbWin {
  public static void Run(string outPath, int seconds) {
    using (var ready = new EventWaitHandle(false, EventResetMode.AutoReset, "DBWIN_BUFFER_READY"))
    using (var data = new EventWaitHandle(false, EventResetMode.AutoReset, "DBWIN_DATA_READY"))
    using (var mmf = MemoryMappedFile.CreateNew("DBWIN_BUFFER", 4096))
    using (var view = mmf.CreateViewStream())
    using (var sw = new StreamWriter(outPath, true)) {
      var rd = new BinaryReader(view);
      var end = DateTime.UtcNow.AddSeconds(seconds);
      ready.Set();
      while (DateTime.UtcNow < end) {
        if (!data.WaitOne(1000)) continue;
        view.Position = 0;
        int pid = rd.ReadInt32();
        byte[] raw = rd.ReadBytes(4092);
        int len = Array.IndexOf(raw, (byte)0); if (len < 0) len = raw.Length;
        sw.WriteLine(DateTime.Now.ToString("HH:mm:ss.fff") + " [" + pid + "] " +
                     System.Text.Encoding.ASCII.GetString(raw, 0, len).TrimEnd());
        sw.Flush();
        ready.Set();
      }
    }
  }
}
"@
[DbWin]::Run($Out, $Seconds)
