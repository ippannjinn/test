using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Windows.Forms;
using NextAI.Common;

namespace NextAI.Setup
{
    static class Program
    {
        [STAThread]
        static int Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            var uninstall = args.Any(a => a.Equals("/uninstall", StringComparison.OrdinalIgnoreCase));
            if (uninstall)
            {
                string ver;
                var info = Shell.ReadExisting(out ver);
                if (info == null)
                {
                    MessageBox.Show("NextAI Platform はインストールされていません。", "アンインストール");
                    return 1;
                }
                var self = System.Reflection.Assembly.GetExecutingAssembly().Location;
                if (!args.Contains("/from-temp") && self.StartsWith(info.InstallDir, StringComparison.OrdinalIgnoreCase))
                {
                    // The installed copy cannot delete its own folder: relaunch from %TEMP%.
                    var tmp = Path.Combine(Path.GetTempPath(), $"NextAI-Uninstall-{Guid.NewGuid():N}.exe");
                    File.Copy(self, tmp, true);
                    Process.Start(new ProcessStartInfo(tmp, "/uninstall /from-temp") { UseShellExecute = false });
                    return 0;
                }
                Application.Run(new UninstallForm(info));
                if (args.Contains("/from-temp"))
                {
                    Process.Start(new ProcessStartInfo("cmd.exe", $"/c ping 127.0.0.1 -n 3 > nul & del \"{self}\"")
                    { CreateNoWindow = true, UseShellExecute = false, WindowStyle = ProcessWindowStyle.Hidden });
                }
                return 0;
            }
            if (!Environment.Is64BitOperatingSystem)
            {
                MessageBox.Show("64bit 版 Windows が必要です。", "NextAI Platform");
                return 1;
            }
            Application.Run(new WizardForm());
            return 0;
        }
    }
}
