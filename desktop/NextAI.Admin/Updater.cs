using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Security.Cryptography;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using NextAI.Common;

namespace NextAI.Admin
{
    /// <summary>One-click update: download the new setup from the release manifest, verify SHA-256,
    /// run it in /update mode (keeps data, settings and models) and close this console.</summary>
    static class Updater
    {
        public static async Task<JObject> CheckAsync(ApiClient api)
        {
            var r = await api.GetAsync("/api/admin/update/check");
            return r.Bool("update_available") ? r : null;
        }

        public static async Task InstallAsync(IWin32Window owner, JObject info)
        {
            var url = info.Str("download_url");
            var sha = info.Str("sha256").ToLowerInvariant();
            var version = info.Str("latest");
            if (!url.StartsWith("https://", StringComparison.OrdinalIgnoreCase) || sha.Length != 64)
                throw new ApiException(0, "update", "更新情報が不完全です (URL または SHA-256 がありません)");
            var msg = $"NextAI Platform を v{info.Str("current")} → v{version} に更新します。\n\n"
                      + "・会話、ファイル、メンバー、設定、ダウンロード済みモデルはすべて保持されます\n"
                      + "・更新中 (数分) はAIサーバーが停止し、実行中のリクエストは中断されます\n"
                      + "・管理者権限の確認 (UAC) が表示されます\n\n続行しますか？";
            if (!Ui.Confirm(owner, msg, "アップデート")) return;

            var dir = Path.Combine(Path.GetTempPath(), "NextAI-Update-" + version);
            Directory.CreateDirectory(dir);
            var exe = Path.Combine(dir, "NextAI-Platform-Setup.exe");
            using (var dlg = new DownloadDialog(version))
            {
                var cts = new CancellationTokenSource();
                dlg.FormClosing += (s, e) => { if (dlg.Tag == null) cts.Cancel(); };
                dlg.Show(owner);
                try
                {
                    await DownloadAsync(url, exe, (done, total) => dlg.Report(done, total), cts.Token);
                    dlg.Status("チェックサムを検証しています…");
                    var got = await Task.Run(() => Sha256(exe));
                    if (got != sha)
                    {
                        File.Delete(exe);
                        throw new ApiException(0, "update", "ダウンロードしたファイルの SHA-256 が一致しません。更新を中止しました。");
                    }
                }
                finally
                {
                    dlg.Tag = "done";
                    dlg.Close();
                }
            }
            try
            {
                Process.Start(new ProcessStartInfo(exe, "/update") { UseShellExecute = true, Verb = "runas" });
            }
            catch (System.ComponentModel.Win32Exception)
            {
                MessageBox.Show(owner, "管理者権限が許可されなかったため、更新を中止しました。", "アップデート");
                return;
            }
            Application.Exit();
        }

        static async Task DownloadAsync(string url, string dest, Action<long, long> progress, CancellationToken ct)
        {
            ServicePointManager.SecurityProtocol |= SecurityProtocolType.Tls12;
            using (var http = new HttpClient { Timeout = TimeSpan.FromMinutes(30) })
            {
                http.DefaultRequestHeaders.UserAgent.ParseAdd("NextAI-Admin-Updater/1.0");
                using (var resp = await http.GetAsync(url, HttpCompletionOption.ResponseHeadersRead, ct))
                {
                    if (!resp.IsSuccessStatusCode)
                        throw new ApiException((int)resp.StatusCode, "update", $"ダウンロードに失敗しました (HTTP {(int)resp.StatusCode})");
                    var total = resp.Content.Headers.ContentLength ?? 0;
                    using (var src = await resp.Content.ReadAsStreamAsync())
                    using (var dst = new FileStream(dest, FileMode.Create, FileAccess.Write))
                    {
                        var buf = new byte[1 << 16];
                        long done = 0;
                        int n;
                        while ((n = await src.ReadAsync(buf, 0, buf.Length, ct)) > 0)
                        {
                            await dst.WriteAsync(buf, 0, n, ct);
                            done += n;
                            progress(done, total);
                        }
                    }
                }
            }
        }

        static string Sha256(string path)
        {
            using (var s = File.OpenRead(path))
            using (var h = SHA256.Create())
                return BitConverter.ToString(h.ComputeHash(s)).Replace("-", "").ToLowerInvariant();
        }

        sealed class DownloadDialog : Form
        {
            readonly ProgressBar bar = new ProgressBar { Left = 16, Top = 44, Width = 368, Height = 18 };
            readonly Label label = new Label { Left = 16, Top = 16, Width = 368, Height = 22 };

            public DownloadDialog(string version)
            {
                Text = $"v{version} をダウンロード中";
                Font = Ui.BaseFont;
                AutoScaleMode = AutoScaleMode.Dpi;
                FormBorderStyle = FormBorderStyle.FixedDialog;
                MaximizeBox = MinimizeBox = false;
                StartPosition = FormStartPosition.CenterParent;
                ClientSize = new Size(400, 80);
                Controls.AddRange(new Control[] { label, bar });
                label.Text = "ダウンロードを開始しています…";
            }

            public void Report(long done, long total)
            {
                if (IsDisposed) return;
                BeginInvoke(new Action(() =>
                {
                    bar.Value = total > 0 ? (int)Math.Min(100, done * 100 / total) : 0;
                    label.Text = $"{Ui.Bytes(done)} / {(total > 0 ? Ui.Bytes(total) : "?")}";
                }));
            }

            public void Status(string s) => label.Text = s;
        }
    }
}
