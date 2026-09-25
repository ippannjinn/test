using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Security.Principal;
using System.ServiceProcess;
using System.Threading.Tasks;

namespace NextAI.Common
{
    public static class WinService
    {
        public static bool IsElevated()
        {
            using (var id = WindowsIdentity.GetCurrent())
                return new WindowsPrincipal(id).IsInRole(WindowsBuiltInRole.Administrator);
        }

        public static string Status(string name = InstallInfo.ServiceName)
        {
            try
            {
                using (var sc = new ServiceController(name))
                    return sc.Status.ToString();
            }
            catch (InvalidOperationException) { return "NotInstalled"; }
        }

        public static bool Exists(string name = InstallInfo.ServiceName) => Status(name) != "NotInstalled";

        /// <summary>Runs sc.exe; elevates through UAC only when this process is not already elevated.</summary>
        public static int Sc(params string[] args)
        {
            var psi = new ProcessStartInfo("sc.exe", ProcessRunner.JoinArgs(args)) { CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden };
            if (IsElevated()) psi.UseShellExecute = false;
            else { psi.UseShellExecute = true; psi.Verb = "runas"; }
            try
            {
                using (var p = Process.Start(psi))
                {
                    p.WaitForExit();
                    return p.ExitCode;
                }
            }
            catch (Win32Exception) { return -1; }
        }

        public static Task<bool> StartAsync(string name = InstallInfo.ServiceName) => Task.Run(() =>
        {
            if (Status(name) == "Running") return true;
            Sc("start", name);
            return WaitFor(name, ServiceControllerStatus.Running, 60);
        });

        public static Task<bool> StopAsync(string name = InstallInfo.ServiceName) => Task.Run(() =>
        {
            if (Status(name) == "Stopped") return true;
            Sc("stop", name);
            return WaitFor(name, ServiceControllerStatus.Stopped, 60);
        });

        public static bool WaitFor(string name, ServiceControllerStatus status, int seconds)
        {
            try
            {
                using (var sc = new ServiceController(name))
                {
                    sc.WaitForStatus(status, TimeSpan.FromSeconds(seconds));
                    return sc.Status == status;
                }
            }
            catch (System.ServiceProcess.TimeoutException) { return false; }
            catch (InvalidOperationException) { return false; }
        }
    }
}
