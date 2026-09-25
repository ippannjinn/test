using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.ServiceProcess;
using System.Text;
using System.Threading;
using NextAI.Common;

namespace NextAI.ServiceHost
{
    static class Program
    {
        static int Main(string[] args)
        {
            if (args.Length > 0 && args[0] == "--console")
            {
                var host = new Supervisor(InstallInfo.LoadNextToExe(), Console.Out);
                Console.CancelKeyPress += (s, e) => { e.Cancel = true; host.Stop(); };
                host.Start();
                host.Join();
                return 0;
            }
            ServiceBase.Run(new HostService());
            return 0;
        }
    }

    sealed class HostService : ServiceBase
    {
        Supervisor sup;

        public HostService()
        {
            ServiceName = InstallInfo.ServiceName;
            CanStop = true;
            CanShutdown = true;
            AutoLog = true;
        }

        protected override void OnStart(string[] args)
        {
            sup = new Supervisor(InstallInfo.LoadNextToExe(), null);
            sup.Start();
        }

        protected override void OnStop()
        {
            RequestAdditionalTime(40000);
            sup?.Stop();
        }

        protected override void OnShutdown() => OnStop();
    }

    /// <summary>Keeps `python -m nextai serve` running; restarts on crash with backoff, on exit code 75 immediately.</summary>
    sealed class Supervisor
    {
        const int RestartCode = 75;
        readonly InstallInfo info;
        readonly TextWriter console;
        readonly ManualResetEvent stopping = new ManualResetEvent(false);
        Thread thread;
        Process current;
        IntPtr job = IntPtr.Zero;
        StreamWriter log;

        public Supervisor(InstallInfo info, TextWriter console)
        {
            this.info = info;
            this.console = console;
        }

        public void Start()
        {
            var logs = Path.Combine(info.DataDir, "logs");
            Directory.CreateDirectory(logs);
            Directory.CreateDirectory(Path.Combine(info.DataDir, "run"));
            var logPath = Path.Combine(logs, "service.log");
            if (File.Exists(logPath) && new FileInfo(logPath).Length > 20 * 1024 * 1024)
                File.Copy(logPath, logPath + ".1", true);
            log = new StreamWriter(new FileStream(logPath, File.Exists(logPath) && new FileInfo(logPath).Length > 20 * 1024 * 1024 ? FileMode.Create : FileMode.Append, FileAccess.Write, FileShare.ReadWrite), new UTF8Encoding(false)) { AutoFlush = true };
            job = JobObject.Create();
            thread = new Thread(Loop) { IsBackground = true, Name = "supervisor" };
            thread.Start();
        }

        void Log(string msg)
        {
            var line = $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} [host] {msg}";
            lock (this)
            {
                try { log?.WriteLine(line); } catch { }
                console?.WriteLine(line);
            }
        }

        void Loop()
        {
            var failures = 0;
            while (!stopping.WaitOne(0))
            {
                var started = DateTime.UtcNow;
                int code;
                try { code = RunOnce(); }
                catch (Exception ex) { Log("failed to start server: " + ex.Message); code = -1; }
                if (stopping.WaitOne(0)) break;
                if (code == RestartCode) { Log("restart requested by server"); failures = 0; continue; }
                if ((DateTime.UtcNow - started).TotalMinutes > 10) failures = 0;
                failures++;
                var delay = failures <= 1 ? 5 : failures == 2 ? 15 : 60;
                Log($"server exited with code {code}; restarting in {delay}s (failure #{failures})");
                if (stopping.WaitOne(TimeSpan.FromSeconds(delay))) break;
            }
        }

        int RunOnce()
        {
            var stopFlag = Path.Combine(info.DataDir, "run", "stop");
            if (File.Exists(stopFlag)) File.Delete(stopFlag);
            var psi = new ProcessStartInfo(info.Python, ProcessRunner.JoinArgs(new[] { "-m", "nextai", "--data-dir", info.DataDir, "serve" }))
            {
                UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true,
                WorkingDirectory = info.DataDir, StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8,
            };
            psi.EnvironmentVariables["PYTHONPATH"] = info.ServerDir;
            psi.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";
            psi.EnvironmentVariables["PYTHONUTF8"] = "1";
            psi.EnvironmentVariables["NEXTAI_DATA_DIR"] = info.DataDir;
            psi.EnvironmentVariables["NEXTAI_UV"] = Path.Combine(info.DataDir, "runtime", "uv", "uv.exe");
            psi.EnvironmentVariables["UV_PYTHON_INSTALL_DIR"] = Path.Combine(info.DataDir, "runtime", "python");
            psi.EnvironmentVariables["UV_CACHE_DIR"] = Path.Combine(info.DataDir, "runtime", "uv-cache");
            Log($"starting server: {info.Python} (data={info.DataDir})");
            using (var p = new Process { StartInfo = psi })
            {
                DataReceivedEventHandler h = (s, e) => { if (e.Data != null) lock (this) { try { log.WriteLine(e.Data); } catch { } console?.WriteLine(e.Data); } };
                p.OutputDataReceived += h;
                p.ErrorDataReceived += h;
                p.Start();
                current = p;
                if (job != IntPtr.Zero) JobObject.Assign(job, p.Handle);
                p.BeginOutputReadLine();
                p.BeginErrorReadLine();
                p.WaitForExit();
                current = null;
                return p.ExitCode;
            }
        }

        public void Stop()
        {
            stopping.Set();
            var p = current;
            if (p != null && !p.HasExited)
            {
                Log("graceful stop requested");
                try { File.WriteAllText(Path.Combine(info.DataDir, "run", "stop"), DateTime.UtcNow.ToString("o")); } catch { }
                if (!p.WaitForExit(30000))
                {
                    Log("server did not stop in time; terminating process tree");
                    JobObject.Terminate(job);
                }
            }
            JobObject.Terminate(job);
            Log("stopped");
        }

        public void Join() => thread?.Join();
    }

    /// <summary>Win32 job object: kills the whole process tree (python + llama-server/sd) with the service,
    /// runs it below normal priority so the desktop stays responsive, and caps committed memory.</summary>
    static class JobObject
    {
        const int JobObjectExtendedLimitInformation = 9;
        const uint KILL_ON_JOB_CLOSE = 0x2000, PRIORITY_CLASS = 0x20, JOB_MEMORY = 0x200;
        const uint BELOW_NORMAL_PRIORITY_CLASS = 0x4000;

        [StructLayout(LayoutKind.Sequential)]
        struct BASIC { public long PerProcessUserTimeLimit, PerJobUserTimeLimit; public uint LimitFlags; public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize; public uint ActiveProcessLimit; public UIntPtr Affinity; public uint PriorityClass, SchedulingClass; }
        [StructLayout(LayoutKind.Sequential)]
        struct IO { public ulong a, b, c, d, e, f; }
        [StructLayout(LayoutKind.Sequential)]
        struct EXTENDED { public BASIC Basic; public IO Io; public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed; }
        [StructLayout(LayoutKind.Sequential)]
        class MEMSTAT { public uint len = 64, load; public ulong total, avail, tpf, apf, tv, av, aev; }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern IntPtr CreateJobObject(IntPtr a, string name);
        [DllImport("kernel32.dll")] static extern bool SetInformationJobObject(IntPtr job, int cls, ref EXTENDED info, int len);
        [DllImport("kernel32.dll")] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
        [DllImport("kernel32.dll")] static extern bool TerminateJobObject(IntPtr job, uint code);
        [DllImport("kernel32.dll")] static extern bool GlobalMemoryStatusEx([In, Out] MEMSTAT m);

        public static IntPtr Create()
        {
            var h = CreateJobObject(IntPtr.Zero, null);
            if (h == IntPtr.Zero) return h;
            var info = new EXTENDED();
            info.Basic.LimitFlags = KILL_ON_JOB_CLOSE | PRIORITY_CLASS;
            info.Basic.PriorityClass = BELOW_NORMAL_PRIORITY_CLASS;
            var m = new MEMSTAT();
            if (GlobalMemoryStatusEx(m) && m.total > 8UL << 30)
            {
                info.Basic.LimitFlags |= JOB_MEMORY;
                info.JobMemoryLimit = new UIntPtr(m.total - (3UL << 30));
            }
            SetInformationJobObject(h, JobObjectExtendedLimitInformation, ref info, Marshal.SizeOf(typeof(EXTENDED)));
            return h;
        }

        public static void Assign(IntPtr job, IntPtr process) => AssignProcessToJobObject(job, process);

        public static void Terminate(IntPtr job)
        {
            if (job != IntPtr.Zero) TerminateJobObject(job, 1);
        }
    }
}
