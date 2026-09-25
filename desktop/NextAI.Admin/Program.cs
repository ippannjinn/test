using System;
using System.Linq;
using System.Threading;
using System.Windows.Forms;
using NextAI.Common;

namespace NextAI.Admin
{
    static class Program
    {
        [STAThread]
        static void Main(string[] args)
        {
            bool created;
            using (var mutex = new Mutex(true, "Local\\NextAI.Admin.SingleInstance", out created))
            {
                if (!created)
                {
                    MessageBox.Show("NextAI 管理コンソールは既に起動しています。", InstallInfo.ProductName, MessageBoxButtons.OK, MessageBoxIcon.Information);
                    return;
                }
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                Application.ThreadException += (s, e) => Ui.Error(null, e.Exception);
                var info = InstallInfo.LoadNextToExe();
                using (var login = new LoginForm(info))
                {
                    if (login.ShowDialog() != DialogResult.OK) return;
                    var start = args.Contains("--diagnostics") ? "server" : null;
                    Application.Run(new MainForm(info, login.Api, start));
                }
                GC.KeepAlive(mutex);
            }
        }
    }
}
