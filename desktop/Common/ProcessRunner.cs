using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace NextAI.Common
{
    public sealed class ProcessResult
    {
        public int ExitCode;
        public string Output = "";
    }

    public static class ProcessRunner
    {
        /// <summary>Runs a process hidden, streaming each output line to onLine. Never uses a shell.</summary>
        public static Task<ProcessResult> RunAsync(string exe, IEnumerable<string> args, Action<string> onLine = null,
            IDictionary<string, string> env = null, string stdin = null, string workDir = null, CancellationToken ct = default)
        {
            return Task.Run(() =>
            {
                var psi = new ProcessStartInfo(exe, JoinArgs(args))
                {
                    UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true,
                    RedirectStandardInput = stdin != null, StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8,
                    WorkingDirectory = workDir ?? Environment.CurrentDirectory,
                };
                if (env != null) foreach (var kv in env) psi.EnvironmentVariables[kv.Key] = kv.Value;
                psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
                psi.EnvironmentVariables["PYTHONUTF8"] = "1";
                var sb = new StringBuilder();
                using (var p = new Process { StartInfo = psi })
                {
                    DataReceivedEventHandler handler = (s, e) =>
                    {
                        if (e.Data == null) return;
                        lock (sb) { if (sb.Length < 2_000_000) sb.AppendLine(e.Data); }
                        try { onLine?.Invoke(e.Data); } catch { }
                    };
                    p.OutputDataReceived += handler;
                    p.ErrorDataReceived += handler;
                    p.Start();
                    p.BeginOutputReadLine();
                    p.BeginErrorReadLine();
                    if (stdin != null)
                    {
                        // .NET Framework has no StandardInputEncoding; write UTF-8 bytes explicitly (Python runs with PYTHONUTF8=1).
                        var bytes = new UTF8Encoding(false).GetBytes(stdin + "\n");
                        p.StandardInput.BaseStream.Write(bytes, 0, bytes.Length);
                        p.StandardInput.BaseStream.Flush();
                        p.StandardInput.Close();
                    }
                    using (ct.Register(() => { try { if (!p.HasExited) p.Kill(); } catch { } }))
                    {
                        p.WaitForExit();
                    }
                    p.WaitForExit();
                    ct.ThrowIfCancellationRequested();
                    return new ProcessResult { ExitCode = p.ExitCode, Output = sb.ToString() };
                }
            }, ct);
        }

        public static string JoinArgs(IEnumerable<string> args)
        {
            var sb = new StringBuilder();
            foreach (var a in args)
            {
                if (sb.Length > 0) sb.Append(' ');
                sb.Append(Quote(a));
            }
            return sb.ToString();
        }

        /// <summary>Windows CommandLineToArgvW-compatible quoting.</summary>
        public static string Quote(string a)
        {
            if (a.Length > 0 && a.IndexOfAny(new[] { ' ', '\t', '"', '\n' }) < 0) return a;
            var sb = new StringBuilder("\"");
            var backslashes = 0;
            foreach (var c in a)
            {
                if (c == '\\') { backslashes++; continue; }
                if (c == '"') sb.Append('\\', backslashes * 2 + 1).Append('"');
                else sb.Append('\\', backslashes).Append(c);
                backslashes = 0;
            }
            sb.Append('\\', backslashes * 2).Append('"');
            return sb.ToString();
        }
    }
}
