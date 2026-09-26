using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using NextAI.Common;

namespace NextAI.Admin
{
    /// <summary>External access through Tailscale Funnel: https://&lt;pc&gt;.&lt;tailnet&gt;.ts.net → http://127.0.0.1:&lt;tunnel_port&gt;.
    /// Friends only need the URL (valid certificate, no app). The server treats that listener as remote-only.</summary>
    static class RemoteAccess
    {
        public const string DownloadUrl = "https://tailscale.com/download/windows";

        public static string Exe
        {
            get
            {
                foreach (var root in new[] { Environment.GetEnvironmentVariable("ProgramW6432"), Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles) })
                {
                    if (string.IsNullOrEmpty(root)) continue;
                    var p = Path.Combine(root, "Tailscale", "tailscale.exe");
                    if (File.Exists(p)) return p;
                }
                return null;
            }
        }

        public sealed class State
        {
            public bool Installed, LoggedIn, FunnelOn;
            public string Host = "", Backend = "";
            public string Url => Host == "" ? "" : "https://" + Host + "/";
        }

        public static async Task<State> StatusAsync()
        {
            var st = new State();
            var exe = Exe;
            if (exe == null) return st;
            st.Installed = true;
            var r = await ProcessRunner.RunAsync(exe, new[] { "status", "--json" }, _ => { });
            var j = Json.ParseObject(r.Output);
            st.Backend = j.Str("BackendState");
            st.LoggedIn = st.Backend == "Running";
            st.Host = j.Obj("Self").Str("DNSName").TrimEnd('.');
            if (st.LoggedIn)
            {
                var f = await ProcessRunner.RunAsync(exe, new[] { "funnel", "status", "--json" }, _ => { });
                var allow = Json.ParseObject(f.Output).Obj("AllowFunnel");
                st.FunnelOn = allow.Keys.Any(k => allow.Bool(k));
            }
            return st;
        }

        /// <summary>Opens the Tailscale login page in the browser (the tray app handles the rest).</summary>
        public static Task LoginAsync(Action<string> log) => RunInteractive(new[] { "up" }, log, TimeSpan.FromMinutes(5));

        public static Task<ProcessResult> EnableAsync(int port, Action<string> log) =>
            RunInteractive(new[] { "funnel", "--bg", $"http://127.0.0.1:{port}" }, log, TimeSpan.FromMinutes(5));

        public static Task<ProcessResult> DisableAsync(Action<string> log) =>
            RunInteractive(new[] { "funnel", "--https=443", "off" }, log, TimeSpan.FromMinutes(1));

        /// <summary>Runs tailscale; any login.tailscale.com link it prints (sign-in, "enable Funnel / HTTPS for your
        /// tailnet") is opened in the browser once, and the command keeps waiting until the user finishes there.</summary>
        static async Task<ProcessResult> RunInteractive(string[] args, Action<string> log, TimeSpan timeout)
        {
            var exe = Exe ?? throw new InvalidOperationException("Tailscale がインストールされていません");
            var opened = new HashSet<string>();
            using (var cts = new CancellationTokenSource(timeout))
            {
                try
                {
                    return await ProcessRunner.RunAsync(exe, args, line =>
                    {
                        log?.Invoke(line);
                        foreach (var word in line.Split(' ', '\t'))
                        {
                            var u = word.Trim();
                            if (u.StartsWith("https://login.tailscale.com/", StringComparison.OrdinalIgnoreCase) && opened.Add(u))
                                try { Process.Start(new ProcessStartInfo(u) { UseShellExecute = true }); } catch { }
                        }
                    }, null, null, null, cts.Token);
                }
                catch (OperationCanceledException)
                {
                    throw new TimeoutException("Tailscale の操作がタイムアウトしました。ブラウザで表示された手順を完了してから、もう一度実行してください。");
                }
            }
        }
    }
}
