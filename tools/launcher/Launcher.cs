// LuvBox.exe: a small launcher with the compressed application appended.
//
// The first start of a version checks the appended archive's SHA-256, unpacks
// it once into %LOCALAPPDATA%\LuvBox\app\<version>\ (through a temporary
// folder and one rename) and starts the application there. Later starts only
// read an 84-byte footer and a marker file, so they cost no unpacking.
//
// Without arguments it shows the splash until the application's window has
// drawn. With arguments (--run-saved, --self-test ...) it shows nothing, passes
// the command line through unchanged and returns the application's exit code.
//
// Built by tools/build_launcher.py with the C# 5 compiler that ships with
// Windows (.NET Framework 4.x): no newer language features in this file.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Drawing.Text;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;

namespace LuvBox
{
    /// <summary>A failure with a fixed message for the person and its own exit code.</summary>
    internal sealed class LauncherError : Exception
    {
        public readonly int Code;

        public LauncherError(int code, string message) : base(message)
        {
            Code = code;
        }
    }

    internal static class ExitCodes
    {
        // The application itself returns 0, 1 or 2; these never overlap.
        public const int NoApplication = 70;
        public const int Damaged = 71;
        public const int UnpackFailed = 72;
        public const int UnsafeLocation = 73;
        public const int Busy = 74;
        public const int StartFailed = 75;
        public const int SameVersionRunning = 76;
        public const int Replaced = 77;
    }

    internal static class Messages
    {
        public const string NoApplication =
            "이 파일에는 앱이 들어 있지 않습니다.\n릴리스에서 LuvBox.exe를 다시 받아 주세요.";
        public const string Damaged =
            "LuvBox.exe가 손상되었습니다. 받는 중에 끊겼거나 파일이 바뀐 것 같습니다.\n릴리스에서 다시 받아 주세요.";
        public const string UnpackFailed =
            "앱을 이 PC에 준비하지 못했습니다.\n디스크 여유 공간(약 300MB)과 사용자 폴더의 AppData\\Local\\LuvBox 쓰기 권한을 확인한 뒤 다시 실행해 주세요.";
        public const string UnsafeLocation =
            "앱을 풀어 둘 폴더가 다른 위치로 연결되어 있어 중단했습니다.\n사용자 폴더의 AppData\\Local\\LuvBox와 그 안의 app 폴더가 일반 폴더인지 확인해 주세요.";
        public const string Busy =
            "다른 LuvBox가 준비를 마치지 못하고 있습니다.\n잠시 뒤 다시 실행해 주세요.";
        public const string StartFailed =
            "앱을 시작하지 못했습니다.\nLuvBox.exe를 다시 받아 실행해 주세요.";
        public const string SameVersionRunning =
            "같은 버전의 LuvBox가 실행 중이라 새 파일로 바꾸지 못했습니다.\n열려 있는 LuvBox를 닫고 다시 실행해 주세요.";
        public const string Replaced =
            "LuvBox.exe가 방금 다른 버전으로 바뀌었습니다.\n다시 실행해 주세요.";
        public const string OldData =
            "이전 이름(XlikeSaver)의 앱 데이터를 LuvBox 폴더로 옮기지 못했습니다.\n실행 중인 이전 버전을 모두 닫고 다시 실행해 주세요.";
    }

    /// <summary>The 84 bytes at the end of LuvBox.exe that locate and vouch for the archive.</summary>
    internal sealed class Footer
    {
        public const int Size = 84;
        static readonly byte[] Head = Encoding.ASCII.GetBytes("LUVBOXAR");
        static readonly byte[] Tail = Encoding.ASCII.GetBytes("RAXOBVUL");
        static readonly Regex VersionShape = new Regex("^[0-9]{1,5}\\.[0-9]{1,5}\\.[0-9]{1,5}$");

        public long Offset;
        public long Length;
        public byte[] Hash;
        public string Version;

        public string HashText
        {
            get
            {
                StringBuilder text = new StringBuilder(64);
                foreach (byte value in Hash)
                    text.Append(value.ToString("x2"));
                return text.ToString();
            }
        }

        /// <summary>Null when the file carries no archive (a bare launcher).</summary>
        public static Footer Read(string path)
        {
            using (FileStream stream = Files.OpenShared(path))
                return Read(stream);
        }

        /// <summary>The same reading through a handle that is already open.</summary>
        public static Footer Read(FileStream stream)
        {
            if (stream.Length < Size + 4096)
                return null;
            byte[] data = new byte[Size];
            stream.Position = stream.Length - Size;
            Files.ReadExactly(stream, data, Size);
            for (int index = 0; index < 8; index++)
            {
                if (data[index] != Head[index] || data[Size - 8 + index] != Tail[index])
                {
                    // Bytes after the program but no footer at the end: a
                    // download that stopped early, not a bare launcher.
                    if (stream.Length > ProgramEnd(stream))
                        throw new LauncherError(ExitCodes.Damaged, Messages.Damaged);
                    return null;
                }
            }
            if (BitConverter.ToUInt32(data, 8) != 1)
                throw new LauncherError(ExitCodes.Damaged, Messages.Damaged);
            Footer footer = new Footer();
            footer.Offset = BitConverter.ToInt64(data, 12);
            footer.Length = BitConverter.ToInt64(data, 20);
            footer.Hash = new byte[32];
            Array.Copy(data, 28, footer.Hash, 0, 32);
            footer.Version = Encoding.ASCII.GetString(data, 60, 16).TrimEnd('\0');
            if (footer.Offset < 4096 || footer.Length < 22
                    || footer.Offset + footer.Length + Size != stream.Length
                    || !VersionShape.IsMatch(footer.Version))
                throw new LauncherError(ExitCodes.Damaged, Messages.Damaged);
            return footer;
        }

        /// <summary>Both describe the same archive.</summary>
        public bool Matches(Footer other)
        {
            return other != null && Offset == other.Offset && Length == other.Length
                && Version == other.Version && HashText == other.HashText;
        }

        /// <summary>Where the launcher program itself ends, from its section table.</summary>
        static long ProgramEnd(FileStream stream)
        {
            try
            {
                byte[] header = new byte[4096];
                stream.Position = 0;
                Files.ReadExactly(stream, header, header.Length);
                int start = BitConverter.ToInt32(header, 0x3C);
                if (start < 0 || start > header.Length - 24 || header[start] != 'P' || header[start + 1] != 'E')
                    return stream.Length;
                int sections = BitConverter.ToUInt16(header, start + 6);
                int table = start + 24 + BitConverter.ToUInt16(header, start + 20);
                long end = 0;
                for (int index = 0; index < sections && table + 40 <= header.Length; index++, table += 40)
                    end = Math.Max(end, (long)BitConverter.ToUInt32(header, table + 20) + BitConverter.ToUInt32(header, table + 16));
                return end > 0 ? end : stream.Length;
            }
            catch (Exception)
            {
                return stream.Length;
            }
        }
    }

    internal static class Files
    {
        /// <summary>Read our own file without blocking an update that renames it.</summary>
        public static FileStream OpenShared(string path)
        {
            return new FileStream(path, FileMode.Open, FileAccess.Read,
                                  FileShare.Read | FileShare.Delete, 256 * 1024);
        }

        public static void ReadExactly(Stream stream, byte[] buffer, int count)
        {
            int done = 0;
            while (done < count)
            {
                int read = stream.Read(buffer, done, count - done);
                if (read <= 0)
                    throw new EndOfStreamException();
                done += read;
            }
        }

        public static bool IsLink(string path)
        {
            // Junctions, symbolic links and other redirections all carry this flag.
            return (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0;
        }

        public static void DeleteTree(string path)
        {
            // Never follow a link out of the folder that is being removed.
            DirectoryInfo folder = new DirectoryInfo(path);
            if (!folder.Exists)
                return;
            if ((folder.Attributes & FileAttributes.ReparsePoint) != 0)
            {
                folder.Delete();
                return;
            }
            foreach (FileInfo file in folder.GetFiles())
            {
                if ((file.Attributes & FileAttributes.ReadOnly) != 0)
                    file.Attributes = FileAttributes.Normal;
                file.Delete();
            }
            foreach (DirectoryInfo child in folder.GetDirectories())
                DeleteTree(child.FullName);
            folder.Delete();
        }
    }

    /// <summary>
    /// A read-only window onto part of a stream, so the archive starts at zero.
    /// Several windows share one open file: each keeps its own place and its
    /// own read-ahead, and takes the gate only while it reads from the file.
    /// </summary>
    internal sealed class SubStream : Stream
    {
        readonly Stream inner;
        readonly object gate;
        readonly long start;
        readonly long length;
        readonly byte[] ahead = new byte[256 * 1024];
        long aheadStart = -1;
        int aheadLength;
        long position;

        public SubStream(Stream inner, object gate, long start, long length)
        {
            this.inner = inner;
            this.gate = gate;
            this.start = start;
            this.length = length;
        }

        public override bool CanRead { get { return true; } }
        public override bool CanSeek { get { return true; } }
        public override bool CanWrite { get { return false; } }
        public override long Length { get { return length; } }

        public override long Position
        {
            get { return position; }
            set { position = value; }
        }

        public override int Read(byte[] buffer, int offset, int count)
        {
            long remaining = length - position;
            if (remaining <= 0 || count <= 0)
                return 0;
            if (count > remaining)
                count = (int)remaining;
            if (aheadStart < 0 || position < aheadStart || position >= aheadStart + aheadLength)
            {
                int wanted = (int)Math.Min(ahead.Length, remaining);
                int filled = 0;
                lock (gate)
                {
                    inner.Position = start + position;
                    while (filled < wanted)
                    {
                        int read = inner.Read(ahead, filled, wanted - filled);
                        if (read <= 0)
                            break;
                        filled += read;
                    }
                }
                if (filled <= 0)
                    return 0;
                aheadStart = position;
                aheadLength = filled;
            }
            int available = (int)(aheadStart + aheadLength - position);
            if (count > available)
                count = available;
            Buffer.BlockCopy(ahead, (int)(position - aheadStart), buffer, offset, count);
            position += count;
            return count;
        }

        public override long Seek(long offset, SeekOrigin origin)
        {
            long target = origin == SeekOrigin.Begin ? offset
                : origin == SeekOrigin.Current ? position + offset : length + offset;
            if (target < 0)
                throw new IOException();
            position = target;
            return position;
        }

        public override void Flush() { }
        public override void SetLength(long value) { throw new NotSupportedException(); }
        public override void Write(byte[] buffer, int offset, int count) { throw new NotSupportedException(); }

        // The file belongs to whoever opened it: closing a window leaves it open.
    }

    /// <summary>Where this version lives on the PC and how it gets there.</summary>
    internal sealed class Installation
    {
        const string Marker = ".luvbox-app";
        const string InnerName = "LuvBox.exe";
        static readonly Regex VersionFolder = new Regex("^[0-9]{1,5}\\.[0-9]{1,5}\\.[0-9]{1,5}$");
        static readonly string[] Reserved = {
            "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
            "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9" };

        public readonly string SelfPath;
        public readonly Footer Footer;
        public readonly string DataRoot;
        public readonly string AppRoot;
        public readonly string VersionRoot;

        // Read by the splash while a worker thread unpacks.
        public long VerifiedBytes;
        public long WrittenBytes;
        public long TotalBytes = 1;
        public volatile bool Waiting;
        public volatile bool Updating;

        // The one handle the archive is checked and then unpacked through. An
        // update may rename LuvBox.exe and put a newer file under its name at
        // any moment; this handle keeps reading the file that was checked.
        FileStream verified;
        readonly object verifiedGate = new object();

        public Installation(string selfPath, Footer footer)
        {
            SelfPath = selfPath;
            Footer = footer;
            string local = Environment.GetEnvironmentVariable("LOCALAPPDATA");
            if (string.IsNullOrEmpty(local) || !Path.IsPathRooted(local))
                local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            if (string.IsNullOrEmpty(local) || !Path.IsPathRooted(local))
                throw new LauncherError(ExitCodes.UnpackFailed, Messages.UnpackFailed);
            local = Path.GetFullPath(local);
            DataRoot = Path.Combine(local, "LuvBox");
            AppRoot = Path.Combine(DataRoot, "app");
            VersionRoot = Path.Combine(AppRoot, footer.Version);
        }

        public string InnerPath
        {
            get { return Path.Combine(VersionRoot, InnerName); }
        }

        public double Progress
        {
            get
            {
                // Checking the archive is quick; unpacking is most of the wait.
                double verified = Footer.Length > 0 ? (double)Interlocked.Read(ref VerifiedBytes) / Footer.Length : 1;
                double written = (double)Interlocked.Read(ref WrittenBytes) / Math.Max(1, Interlocked.Read(ref TotalBytes));
                return Math.Max(0, Math.Min(1, 0.08 * verified + 0.92 * written));
            }
        }

        /// <summary>This exact archive is already unpacked and can be started.</summary>
        public bool Ready()
        {
            try
            {
                if (!Directory.Exists(VersionRoot) || Files.IsLink(DataRoot) || Files.IsLink(AppRoot)
                        || Files.IsLink(VersionRoot))
                    return false;
                return MarkerHash(VersionRoot) == Footer.HashText && File.Exists(InnerPath);
            }
            catch (IOException) { return false; }
            catch (UnauthorizedAccessException) { return false; }
        }

        static string MarkerHash(string folder)
        {
            string path = Path.Combine(folder, Marker);
            if (!File.Exists(path))
                return null;
            foreach (string line in File.ReadAllLines(path, Encoding.UTF8))
            {
                if (line.StartsWith("payload=", StringComparison.Ordinal))
                    return line.Substring(8).Trim();
            }
            return null;
        }

        /// <summary>Unpack once. Safe to call from several launchers at the same time.</summary>
        public void Prepare()
        {
            PrepareFolders();
            using (FileStream gate = AcquireGate())
            {
                // Another launcher may have finished while this one waited.
                if (Ready())
                    return;
                RemoveLeftovers();
                Updating = OtherVersionExists();
                using (FileStream source = OpenSelf())
                {
                    // This handle stays open from the check to the last file and
                    // denies writers, and nothing below opens LuvBox.exe by name
                    // again, so what was checked is what is unpacked.
                    Verify(source);
                    verified = source;
                    string temporary = Path.Combine(AppRoot, ".new-" + Process.GetCurrentProcess().Id.ToString("x")
                                                    + "-" + (DateTime.UtcNow.Ticks & 0xffffff).ToString("x"));
                    try
                    {
                        Directory.CreateDirectory(temporary);
                        Extract(temporary);
                        File.WriteAllText(Path.Combine(temporary, Marker),
                                          "version=" + Footer.Version + "\npayload=" + Footer.HashText + "\n",
                                          new UTF8Encoding(false));
                        Publish(temporary);
                    }
                    catch (LauncherError)
                    {
                        TryDelete(temporary);
                        throw;
                    }
                    catch (Exception)
                    {
                        TryDelete(temporary);
                        throw new LauncherError(ExitCodes.UnpackFailed, Messages.UnpackFailed);
                    }
                    finally
                    {
                        verified = null;
                    }
                }
                RemoveOlderVersions();
            }
        }

        void PrepareFolders()
        {
            try
            {
                string previous = Path.Combine(Path.GetDirectoryName(DataRoot), "XlikeSaver");
                if (Directory.Exists(previous) && !Files.IsLink(previous))
                {
                    // The application adopts data kept under its former name by
                    // renaming that folder. It can only do so while this one is
                    // absent or empty, so do the same move before creating "app".
                    bool empty = !Directory.Exists(DataRoot)
                        || (!Files.IsLink(DataRoot) && Directory.GetFileSystemEntries(DataRoot).Length == 0);
                    if (empty)
                    {
                        try
                        {
                            if (Directory.Exists(DataRoot))
                                Directory.Delete(DataRoot);
                            Directory.Move(previous, DataRoot);
                        }
                        catch (Exception)
                        {
                            if (Directory.Exists(previous))
                                throw new LauncherError(ExitCodes.UnpackFailed, Messages.OldData);
                        }
                    }
                }
                Directory.CreateDirectory(DataRoot);
                if (Files.IsLink(DataRoot))
                    throw new LauncherError(ExitCodes.UnsafeLocation, Messages.UnsafeLocation);
                Directory.CreateDirectory(AppRoot);
                if (Files.IsLink(AppRoot))
                    throw new LauncherError(ExitCodes.UnsafeLocation, Messages.UnsafeLocation);
                if (Directory.Exists(VersionRoot) && Files.IsLink(VersionRoot))
                    throw new LauncherError(ExitCodes.UnsafeLocation, Messages.UnsafeLocation);
            }
            catch (LauncherError) { throw; }
            catch (Exception)
            {
                throw new LauncherError(ExitCodes.UnpackFailed, Messages.UnpackFailed);
            }
        }

        FileStream AcquireGate()
        {
            // One unpacker at a time for this user, across sessions. The handle
            // is released when its process ends, however it ends.
            string path = Path.Combine(AppRoot, ".lock");
            DateTime limit = DateTime.UtcNow.AddMinutes(4);
            while (true)
            {
                try
                {
                    FileStream gate = new FileStream(path, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
                    Waiting = false;
                    return gate;
                }
                catch (IOException)
                {
                    if (Ready())
                    {
                        // Finished by the other launcher; no need for the gate.
                        Waiting = false;
                        return null;
                    }
                    if (DateTime.UtcNow > limit)
                        throw new LauncherError(ExitCodes.Busy, Messages.Busy);
                    Waiting = true;
                    Thread.Sleep(200);
                }
                catch (UnauthorizedAccessException)
                {
                    throw new LauncherError(ExitCodes.UnpackFailed, Messages.UnpackFailed);
                }
            }
        }

        bool OtherVersionExists()
        {
            try
            {
                foreach (string folder in Directory.GetDirectories(AppRoot))
                {
                    string name = Path.GetFileName(folder);
                    if (VersionFolder.IsMatch(name) && name != Footer.Version && File.Exists(Path.Combine(folder, Marker)))
                        return true;
                }
            }
            catch (Exception) { }
            return false;
        }

        void Verify(FileStream source)
        {
            byte[] buffer = new byte[1024 * 1024];
            long remaining = Footer.Length;
            source.Position = Footer.Offset;
            using (SHA256 hash = new SHA256Cng())
            {
                while (remaining > 0)
                {
                    int read = source.Read(buffer, 0, (int)Math.Min(buffer.Length, remaining));
                    if (read <= 0)
                        throw new LauncherError(ExitCodes.Damaged, Messages.Damaged);
                    hash.TransformBlock(buffer, 0, read, null, 0);
                    remaining -= read;
                    Interlocked.Add(ref VerifiedBytes, read);
                }
                hash.TransformFinalBlock(buffer, 0, 0);
                byte[] actual = hash.Hash;
                for (int index = 0; index < 32; index++)
                {
                    if (actual[index] != Footer.Hash[index])
                        throw new LauncherError(ExitCodes.Damaged, Messages.Damaged);
                }
            }
        }

        /// <summary>Open LuvBox.exe and make sure it is still the file whose footer was read at start.</summary>
        FileStream OpenSelf()
        {
            FileStream source;
            try
            {
                source = Files.OpenShared(SelfPath);
            }
            catch (Exception)
            {
                throw new LauncherError(ExitCodes.UnpackFailed, Messages.UnpackFailed);
            }
            try
            {
                // An update that landed since then put another archive under this
                // name. Its offsets are not ours: say so instead of "damaged".
                if (!Footer.Matches(Footer.Read(source)))
                    throw new LauncherError(ExitCodes.Replaced, Messages.Replaced);
                return source;
            }
            catch (LauncherError)
            {
                source.Dispose();
                throw;
            }
            catch (Exception)
            {
                source.Dispose();
                throw new LauncherError(ExitCodes.Damaged, Messages.Damaged);
            }
        }

        ZipArchive OpenArchive()
        {
            return new ZipArchive(new SubStream(verified, verifiedGate, Footer.Offset, Footer.Length),
                                  ZipArchiveMode.Read, false);
        }

        void Extract(string root)
        {
            string prefix = root.EndsWith("\\", StringComparison.Ordinal) ? root : root + "\\";
            List<string> targets = new List<string>();
            long total = 0;
            using (ZipArchive archive = OpenArchive())
            {
                HashSet<string> seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                PropertyInfo attributes = typeof(ZipArchiveEntry).GetProperty("ExternalAttributes");
                foreach (ZipArchiveEntry entry in archive.Entries)
                {
                    string relative = SafeRelative(entry.FullName);
                    string target = Path.GetFullPath(Path.Combine(root, relative));
                    if (!target.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) || !seen.Add(target))
                        throw new LauncherError(ExitCodes.Damaged, Messages.Damaged);
                    if (attributes != null)
                    {
                        // A Unix symbolic link entry: never part of this application.
                        int mode = (Convert.ToInt32(attributes.GetValue(entry, null)) >> 16) & 0xF000;
                        if (mode == 0xA000)
                            throw new LauncherError(ExitCodes.Damaged, Messages.Damaged);
                    }
                    targets.Add(entry.FullName.EndsWith("/", StringComparison.Ordinal) ? null : target);
                    total += entry.Length;
                }
            }
            if (targets.Count == 0)
                throw new LauncherError(ExitCodes.Damaged, Messages.Damaged);
            Interlocked.Exchange(ref TotalBytes, Math.Max(1, total));

            int next = -1;
            Exception failure = null;
            int workers = Math.Max(1, Math.Min(4, Environment.ProcessorCount));
            Thread[] threads = new Thread[workers];
            for (int worker = 0; worker < workers; worker++)
            {
                threads[worker] = new Thread(delegate()
                {
                    try
                    {
                        byte[] buffer = new byte[256 * 1024];
                        using (ZipArchive archive = OpenArchive())
                        {
                            while (failure == null)
                            {
                                int index = Interlocked.Increment(ref next);
                                if (index >= targets.Count)
                                    break;
                                ZipArchiveEntry entry = archive.Entries[index];
                                string target = targets[index];
                                if (target == null)
                                {
                                    Directory.CreateDirectory(Path.GetFullPath(Path.Combine(root, SafeRelative(entry.FullName))));
                                    continue;
                                }
                                Directory.CreateDirectory(Path.GetDirectoryName(target));
                                long written = 0;
                                using (Stream input = entry.Open())
                                using (FileStream output = new FileStream(target, FileMode.CreateNew, FileAccess.Write,
                                                                          FileShare.None, 256 * 1024))
                                {
                                    int read;
                                    while ((read = input.Read(buffer, 0, buffer.Length)) > 0)
                                    {
                                        output.Write(buffer, 0, read);
                                        written += read;
                                        Interlocked.Add(ref WrittenBytes, read);
                                    }
                                }
                                if (written != entry.Length)
                                    throw new InvalidDataException();
                            }
                        }
                    }
                    catch (Exception error)
                    {
                        Interlocked.CompareExchange(ref failure, error, null);
                    }
                });
                threads[worker].IsBackground = true;
                threads[worker].Start();
            }
            foreach (Thread thread in threads)
                thread.Join();
            if (failure != null)
            {
                if (failure is LauncherError)
                    throw failure;
                if (failure is InvalidDataException)
                    throw new LauncherError(ExitCodes.Damaged, Messages.Damaged);
                throw new LauncherError(ExitCodes.UnpackFailed, Messages.UnpackFailed);
            }
        }

        /// <summary>An archive name as a relative Windows path, or a refusal.</summary>
        static string SafeRelative(string name)
        {
            if (string.IsNullOrEmpty(name) || name.Length > 240 || name[0] == '/' || name.IndexOf('\\') >= 0
                    || name.IndexOf(':') >= 0)
                throw new LauncherError(ExitCodes.Damaged, Messages.Damaged);
            string trimmed = name.EndsWith("/", StringComparison.Ordinal) ? name.Substring(0, name.Length - 1) : name;
            string[] parts = trimmed.Split('/');
            foreach (string part in parts)
            {
                if (part.Length == 0 || part == "." || part == ".." || part.EndsWith(" ", StringComparison.Ordinal)
                        || part.EndsWith(".", StringComparison.Ordinal))
                    throw new LauncherError(ExitCodes.Damaged, Messages.Damaged);
                foreach (char letter in part)
                {
                    if (letter < ' ' || letter == '<' || letter == '>' || letter == '"' || letter == '|'
                            || letter == '?' || letter == '*')
                        throw new LauncherError(ExitCodes.Damaged, Messages.Damaged);
                }
                int dot = part.IndexOf('.');
                string stem = (dot < 0 ? part : part.Substring(0, dot)).ToUpperInvariant();
                if (Array.IndexOf(Reserved, stem) >= 0)
                    throw new LauncherError(ExitCodes.Damaged, Messages.Damaged);
            }
            return string.Join("\\", parts);
        }

        void Publish(string temporary)
        {
            if (Directory.Exists(VersionRoot))
            {
                // Same version number, different archive (an interrupted or a
                // rebuilt copy). A folder in use cannot be renamed, which is how
                // a running application is noticed before anything is removed.
                string aside = Path.Combine(AppRoot, ".old-" + (DateTime.UtcNow.Ticks & 0xffffff).ToString("x"));
                if (!MoveFolder(VersionRoot, aside, 3))
                    throw new LauncherError(ExitCodes.SameVersionRunning, Messages.SameVersionRunning);
                if (!MoveFolder(temporary, VersionRoot, 20))
                    throw new LauncherError(ExitCodes.UnpackFailed, Messages.UnpackFailed);
                TryDelete(aside);
                return;
            }
            if (!MoveFolder(temporary, VersionRoot, 20))
                throw new LauncherError(ExitCodes.UnpackFailed, Messages.UnpackFailed);
        }

        /// <summary>Rename a folder, waiting out a scanner that still holds a file in it.</summary>
        static bool MoveFolder(string source, string target, int seconds)
        {
            // Windows refuses to rename a folder while any file inside is open.
            // Security software often opens files right after they are written.
            DateTime limit = DateTime.UtcNow.AddSeconds(seconds);
            while (true)
            {
                try
                {
                    Directory.Move(source, target);
                    return true;
                }
                catch (IOException)
                {
                    if (Directory.Exists(target) || DateTime.UtcNow > limit)
                        return false;
                }
                catch (UnauthorizedAccessException)
                {
                    if (DateTime.UtcNow > limit)
                        return false;
                }
                Thread.Sleep(150);
            }
        }

        void RemoveLeftovers()
        {
            // Only reached while holding the gate: any half-made folder is stale.
            try
            {
                foreach (string folder in Directory.GetDirectories(AppRoot))
                {
                    string name = Path.GetFileName(folder);
                    if (name.StartsWith(".new-", StringComparison.Ordinal) || name.StartsWith(".old-", StringComparison.Ordinal))
                        TryDelete(folder);
                }
            }
            catch (Exception) { }
        }

        void RemoveOlderVersions()
        {
            // Keep this version and the one before it; an update that turns out
            // badly can still be left by running the earlier LuvBox.exe.
            try
            {
                Version current = new Version(Footer.Version);
                List<Version> older = new List<Version>();
                foreach (string folder in Directory.GetDirectories(AppRoot))
                {
                    string name = Path.GetFileName(folder);
                    if (!VersionFolder.IsMatch(name) || Files.IsLink(folder) || !File.Exists(Path.Combine(folder, Marker)))
                        continue;
                    Version found = new Version(name);
                    if (found < current)
                        older.Add(found);
                }
                older.Sort();
                for (int index = 0; index < older.Count - 1; index++)
                {
                    string folder = Path.Combine(AppRoot, older[index].ToString(3));
                    string aside = Path.Combine(AppRoot, ".old-" + older[index].ToString(3).Replace('.', '-'));
                    try
                    {
                        // Renaming fails while that version is still running.
                        Directory.Move(folder, aside);
                    }
                    catch (Exception) { continue; }
                    TryDelete(aside);
                }
            }
            catch (Exception) { }
        }

        static void TryDelete(string folder)
        {
            try { Files.DeleteTree(folder); }
            catch (Exception) { }
        }
    }

    internal static class Native
    {
        [StructLayout(LayoutKind.Sequential)]
        public struct POINT { public int X; public int Y; }

        [StructLayout(LayoutKind.Sequential)]
        public struct SIZE { public int CX; public int CY; }

        [StructLayout(LayoutKind.Sequential, Pack = 1)]
        public struct BLENDFUNCTION
        {
            public byte BlendOp;
            public byte BlendFlags;
            public byte SourceConstantAlpha;
            public byte AlphaFormat;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct BITMAPINFOHEADER
        {
            public int biSize;
            public int biWidth;
            public int biHeight;
            public short biPlanes;
            public short biBitCount;
            public int biCompression;
            public int biSizeImage;
            public int biXPelsPerMeter;
            public int biYPelsPerMeter;
            public int biClrUsed;
            public int biClrImportant;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct JOBOBJECT_BASIC_LIMIT_INFORMATION
        {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct IO_COUNTERS
        {
            public ulong ReadOperationCount;
            public ulong WriteOperationCount;
            public ulong OtherOperationCount;
            public ulong ReadTransferCount;
            public ulong WriteTransferCount;
            public ulong OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        {
            public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
            public IO_COUNTERS IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [DllImport("user32.dll", SetLastError = true)]
        public static extern bool UpdateLayeredWindow(IntPtr window, IntPtr destination, ref POINT position,
            ref SIZE size, IntPtr source, ref POINT sourcePosition, int key, ref BLENDFUNCTION blend, int flags);

        [DllImport("user32.dll")]
        public static extern IntPtr GetDC(IntPtr window);

        [DllImport("user32.dll")]
        public static extern int ReleaseDC(IntPtr window, IntPtr context);

        [DllImport("user32.dll")]
        public static extern bool AllowSetForegroundWindow(int processId);

        [DllImport("gdi32.dll")]
        public static extern IntPtr CreateCompatibleDC(IntPtr context);

        [DllImport("gdi32.dll")]
        public static extern bool DeleteDC(IntPtr context);

        [DllImport("gdi32.dll")]
        public static extern IntPtr SelectObject(IntPtr context, IntPtr item);

        [DllImport("gdi32.dll")]
        public static extern bool DeleteObject(IntPtr item);

        [DllImport("gdi32.dll")]
        public static extern IntPtr CreateDIBSection(IntPtr context, ref BITMAPINFOHEADER header, uint usage,
            out IntPtr bits, IntPtr section, uint offset);

        [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
        public static extern int SetCurrentProcessExplicitAppUserModelID(string identifier);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        public static extern IntPtr CreateJobObject(IntPtr attributes, string name);

        [DllImport("kernel32.dll")]
        public static extern bool SetInformationJobObject(IntPtr job, int kind, ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION data, int length);

        [DllImport("kernel32.dll")]
        public static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    }

    /// <summary>Layout and wording from splash.ini, shared with tools/draw_splash.py.</summary>
    internal sealed class SplashSettings
    {
        readonly Dictionary<string, List<string>> values = new Dictionary<string, List<string>>();

        public SplashSettings(Stream stream)
        {
            using (StreamReader reader = new StreamReader(stream, Encoding.UTF8))
            {
                string raw;
                while ((raw = reader.ReadLine()) != null)
                {
                    string line = raw.Trim();
                    int split = line.IndexOf('=');
                    if (line.Length == 0 || line[0] == '#' || split < 1)
                        continue;
                    string key = line.Substring(0, split).Trim();
                    if (!values.ContainsKey(key))
                        values[key] = new List<string>();
                    values[key].Add(line.Substring(split + 1).Trim());
                }
            }
        }

        public string Text(string key)
        {
            return values.ContainsKey(key) ? values[key][0] : "";
        }

        public List<string> Lines(string key)
        {
            return values.ContainsKey(key) ? values[key] : new List<string>();
        }

        public int[] Numbers(string key)
        {
            string[] parts = Text(key).Split(',');
            int[] result = new int[parts.Length];
            for (int index = 0; index < parts.Length; index++)
                int.TryParse(parts[index].Trim(), out result[index]);
            return result;
        }

        public int Number(string key)
        {
            return Numbers(key)[0];
        }
    }

    /// <summary>What the splash's worker thread reports to its drawing timer.</summary>
    internal sealed class Session
    {
        public Installation Installation;
        public string Arguments;
        public volatile bool Unpacking;
        public volatile bool Finished;
        public volatile LauncherError Error;
        public volatile int ExitCode;
    }

    /// <summary>A borderless card with soft edges, drawn by hand and blended by Windows.</summary>
    internal sealed class Splash : Form
    {
        readonly Session session;
        readonly SplashSettings settings;
        readonly double scale;
        readonly int pixelWidth;
        readonly int pixelHeight;
        readonly Bitmap card;
        readonly Bitmap glow;
        readonly Bitmap mark;
        readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
        readonly Stopwatch clock = Stopwatch.StartNew();
        readonly PrivateFontCollection fonts = new PrivateFontCollection();
        IntPtr fontMemory = IntPtr.Zero;
        Font statusFont;
        Font tipFont;
        Font versionFont;

        IntPtr screen = IntPtr.Zero;
        IntPtr memory = IntPtr.Zero;
        IntPtr surface = IntPtr.Zero;
        IntPtr previous = IntPtr.Zero;
        Bitmap canvas;

        string status = "";
        string statusBefore = "";
        double statusChanged = -10;
        List<string> tips = new List<string>();
        int tipIndex;
        string tipBefore = "";
        double tipChanged = -10;
        double shown;
        double finishedAt = -1;
        double openSince = -1;
        bool closing;

        public Splash(Session session)
        {
            this.session = session;
            Assembly assembly = Assembly.GetExecutingAssembly();
            settings = new SplashSettings(assembly.GetManifestResourceStream("LuvBox.splash.ini"));
            using (Graphics probe = Graphics.FromHwnd(IntPtr.Zero))
                scale = Math.Max(1.0, probe.DpiX / 96.0);
            pixelWidth = (int)Math.Round(settings.Number("width") * scale);
            pixelHeight = (int)Math.Round(settings.Number("height") * scale);
            card = Layer(assembly, "LuvBox.card.png");
            glow = Layer(assembly, "LuvBox.glow.png");
            mark = Layer(assembly, "LuvBox.mark.png");
            LoadFonts(assembly);

            Text = "LuvBox";
            FormBorderStyle = FormBorderStyle.None;
            StartPosition = FormStartPosition.Manual;
            ShowInTaskbar = true;
            Rectangle area = Screen.FromPoint(Cursor.Position).WorkingArea;
            Bounds = new Rectangle(area.Left + (area.Width - pixelWidth) / 2,
                                   area.Top + (area.Height - pixelHeight) / 2, pixelWidth, pixelHeight);
            try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); }
            catch (Exception) { }
            timer.Interval = 33;
            timer.Tick += delegate { Advance(); };
        }

        protected override CreateParams CreateParams
        {
            get
            {
                CreateParams parameters = base.CreateParams;
                parameters.ExStyle |= 0x00080000;  // WS_EX_LAYERED: per-pixel transparency
                return parameters;
            }
        }

        protected override void WndProc(ref Message message)
        {
            base.WndProc(ref message);
            // Let the card be dragged out of the way during a long first start.
            if (message.Msg == 0x0084 && message.Result == (IntPtr)1)
                message.Result = (IntPtr)2;
        }

        Bitmap Layer(Assembly assembly, string name)
        {
            // Scale each 2x layer to this display once; every frame then copies pixels as they are.
            Bitmap result = new Bitmap(pixelWidth, pixelHeight, PixelFormat.Format32bppPArgb);
            using (Stream stream = assembly.GetManifestResourceStream(name))
            using (Image source = Image.FromStream(stream))
            using (Graphics graphics = Graphics.FromImage(result))
            {
                graphics.InterpolationMode = InterpolationMode.HighQualityBicubic;
                graphics.PixelOffsetMode = PixelOffsetMode.HighQuality;
                graphics.CompositingMode = CompositingMode.SourceCopy;
                using (ImageAttributes wrap = new ImageAttributes())
                {
                    wrap.SetWrapMode(WrapMode.TileFlipXY);
                    graphics.DrawImage(source, new Rectangle(0, 0, pixelWidth, pixelHeight),
                                       0, 0, source.Width, source.Height, GraphicsUnit.Pixel, wrap);
                }
            }
            return result;
        }

        void LoadFonts(Assembly assembly)
        {
            FontFamily family = null;
            try
            {
                using (Stream stream = assembly.GetManifestResourceStream("LuvBox.font.ttf"))
                {
                    byte[] data = new byte[stream.Length];
                    Files.ReadExactly(stream, data, data.Length);
                    // GDI+ keeps reading this memory for as long as the font is used.
                    fontMemory = Marshal.AllocCoTaskMem(data.Length);
                    Marshal.Copy(data, 0, fontMemory, data.Length);
                    fonts.AddMemoryFont(fontMemory, data.Length);
                    if (fonts.Families.Length > 0)
                        family = fonts.Families[0];
                }
            }
            catch (Exception) { family = null; }
            if (family == null)
                family = new FontFamily("Malgun Gothic");
            statusFont = new Font(family, (float)(settings.Number("status_size") * scale), FontStyle.Regular, GraphicsUnit.Pixel);
            tipFont = new Font(family, (float)(settings.Number("tip_size") * scale), FontStyle.Regular, GraphicsUnit.Pixel);
            versionFont = new Font(family, (float)(settings.Number("version_size") * scale), FontStyle.Regular, GraphicsUnit.Pixel);
        }

        protected override void OnLoad(EventArgs e)
        {
            base.OnLoad(e);
            screen = Native.GetDC(IntPtr.Zero);
            memory = Native.CreateCompatibleDC(screen);
            Native.BITMAPINFOHEADER header = new Native.BITMAPINFOHEADER();
            header.biSize = Marshal.SizeOf(typeof(Native.BITMAPINFOHEADER));
            header.biWidth = pixelWidth;
            header.biHeight = -pixelHeight;  // top-down rows, like a Bitmap
            header.biPlanes = 1;
            header.biBitCount = 32;
            IntPtr bits;
            surface = Native.CreateDIBSection(screen, ref header, 0, out bits, IntPtr.Zero, 0);
            previous = Native.SelectObject(memory, surface);
            // Draw straight into the memory Windows blends from: no copy per frame.
            canvas = new Bitmap(pixelWidth, pixelHeight, pixelWidth * 4, PixelFormat.Format32bppPArgb, bits);
            Advance();
            timer.Start();
            Thread worker = new Thread(Work);
            worker.IsBackground = true;
            worker.Start();
        }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            // Alt+F4 on the splash would leave a half-finished start behind.
            if (!closing && e.CloseReason == CloseReason.UserClosing)
                e.Cancel = true;
            base.OnFormClosing(e);
        }

        protected override void OnFormClosed(FormClosedEventArgs e)
        {
            timer.Stop();
            if (canvas != null)
                canvas.Dispose();
            if (memory != IntPtr.Zero)
            {
                Native.SelectObject(memory, previous);
                Native.DeleteDC(memory);
            }
            if (surface != IntPtr.Zero)
                Native.DeleteObject(surface);
            if (screen != IntPtr.Zero)
                Native.ReleaseDC(IntPtr.Zero, screen);
            base.OnFormClosed(e);
        }

        void Work()
        {
            try
            {
                Installation installation = session.Installation;
                if (!installation.Ready())
                {
                    session.Unpacking = true;
                    installation.Prepare();
                    session.Unpacking = false;
                }
                session.ExitCode = Program.RunInteractive(installation, session.Arguments);
            }
            catch (LauncherError error)
            {
                session.Error = error;
            }
            catch (Exception)
            {
                session.Error = new LauncherError(ExitCodes.StartFailed, Messages.StartFailed);
            }
            session.Unpacking = false;
            session.Finished = true;
        }

        void SetStatus(string text, double now)
        {
            if (text == status)
                return;
            statusBefore = status;
            status = text;
            statusChanged = now;
        }

        void SetTips(List<string> lines, double now)
        {
            if (ReferenceEquals(lines, tips) || lines.Count == 0)
                return;
            tipBefore = tips.Count > 0 ? tips[tipIndex % tips.Count] : "";
            tips = lines;
            tipIndex = 0;
            tipChanged = now;
        }

        void Advance()
        {
            double now = clock.Elapsed.TotalSeconds;
            Installation installation = session.Installation;
            if (session.Error != null)
            {
                closing = true;
                Close();
                return;
            }
            if (session.Unpacking)
            {
                SetStatus(settings.Text(installation.Waiting ? "status.wait"
                                        : installation.Updating ? "status.update" : "status.first"), now);
                SetTips(settings.Lines("tip.unpack"), now);
                shown += (installation.Progress - shown) * 0.25;
            }
            else
            {
                SetStatus(settings.Text("status.open"), now);
                SetTips(settings.Lines("tip.open"), now);
                if (openSince < 0)
                    openSince = now;
            }
            if (tips.Count > 1 && now - tipChanged > 2.6)
            {
                tipBefore = tips[tipIndex % tips.Count];
                tipIndex = (tipIndex + 1) % tips.Count;
                tipChanged = now;
            }

            double opacity = Math.Min(1.0, now / 0.18);
            if (session.Finished)
            {
                if (finishedAt < 0)
                    finishedAt = now;
                opacity = Math.Min(opacity, 1.0 - (now - finishedAt) / 0.22);
                if (opacity <= 0)
                {
                    closing = true;
                    Close();
                    return;
                }
            }
            Render(now);
            Present((byte)Math.Round(255 * Math.Max(0, Math.Min(1, opacity))));
        }

        static double Ease(double value)
        {
            value = Math.Max(0, Math.Min(1, value));
            return value * value * (3 - 2 * value);
        }

        void Render(double now)
        {
            using (Graphics graphics = Graphics.FromImage(canvas))
            {
                graphics.CompositingMode = CompositingMode.SourceCopy;
                graphics.DrawImageUnscaled(card, 0, 0);
                graphics.CompositingMode = CompositingMode.SourceOver;
                // The light behind the heart breathes slowly.
                float breath = (float)(0.62 + 0.30 * Math.Sin(now * 2 * Math.PI / 3.2));
                using (ImageAttributes attributes = new ImageAttributes())
                {
                    ColorMatrix matrix = new ColorMatrix();
                    matrix.Matrix33 = breath;
                    attributes.SetColorMatrix(matrix);
                    graphics.DrawImage(glow, new Rectangle(0, 0, pixelWidth, pixelHeight), 0, 0, pixelWidth, pixelHeight,
                                       GraphicsUnit.Pixel, attributes);
                }
                graphics.DrawImageUnscaled(mark, 0, 0);

                graphics.SmoothingMode = SmoothingMode.AntiAlias;
                graphics.PixelOffsetMode = PixelOffsetMode.HighQuality;
                graphics.TextRenderingHint = TextRenderingHint.AntiAlias;
                double statusFade = Ease((now - statusChanged) / 0.30);
                DrawLine(graphics, statusBefore, statusFont, settings.Number("status_y"), Color.FromArgb(244, 241, 241), 1 - statusFade);
                DrawLine(graphics, status, statusFont, settings.Number("status_y"), Color.FromArgb(244, 241, 241), statusFade);
                double tipFade = Ease((now - tipChanged) / 0.35);
                DrawLine(graphics, tipBefore, tipFont, settings.Number("tip_y"), Color.FromArgb(142, 132, 134), 1 - tipFade);
                if (tips.Count > 0)
                    DrawLine(graphics, tips[tipIndex % tips.Count], tipFont, settings.Number("tip_y"),
                             Color.FromArgb(142, 132, 134), tipFade);
                DrawBar(graphics, now);
                using (Brush brush = new SolidBrush(Color.FromArgb(104, 96, 98)))
                using (StringFormat format = new StringFormat(StringFormat.GenericTypographic))
                {
                    format.Alignment = StringAlignment.Far;
                    format.LineAlignment = StringAlignment.Center;
                    graphics.DrawString(session.Installation.Footer.Version, versionFont, brush,
                                        (float)(settings.Number("version_right") * scale),
                                        (float)(settings.Number("version_y") * scale), format);
                }
            }
        }

        void DrawLine(Graphics graphics, string text, Font font, int centerY, Color color, double strength)
        {
            if (string.IsNullOrEmpty(text) || strength <= 0.01)
                return;
            using (Brush brush = new SolidBrush(Color.FromArgb((int)Math.Round(255 * Math.Min(1, strength)), color)))
            using (StringFormat format = new StringFormat(StringFormat.GenericTypographic))
            {
                format.Alignment = StringAlignment.Center;
                format.LineAlignment = StringAlignment.Center;
                format.FormatFlags |= StringFormatFlags.NoWrap;
                graphics.DrawString(text, font, brush, pixelWidth / 2f, (float)(centerY * scale), format);
            }
        }

        static GraphicsPath Pill(RectangleF bounds)
        {
            GraphicsPath path = new GraphicsPath();
            float diameter = Math.Min(bounds.Height, bounds.Width);
            if (diameter <= 0.01f)
                return path;
            path.AddArc(bounds.Left, bounds.Top, diameter, diameter, 90, 180);
            path.AddArc(bounds.Right - diameter, bounds.Top, diameter, diameter, 270, 180);
            path.CloseFigure();
            return path;
        }

        void DrawBar(Graphics graphics, double now)
        {
            int[] bar = settings.Numbers("bar");
            RectangleF track = new RectangleF((float)(bar[0] * scale), (float)(bar[1] * scale),
                                              (float)(bar[2] * scale), (float)(bar[3] * scale));
            using (GraphicsPath path = Pill(track))
            using (Brush brush = new SolidBrush(Color.FromArgb(42, 42, 42)))
                graphics.FillPath(brush, path);
            RectangleF fill;
            if (session.Unpacking)
            {
                fill = new RectangleF(track.Left, track.Top, (float)Math.Max(track.Height, track.Width * shown), track.Height);
            }
            else
            {
                // Nothing to count while the application loads: a short light
                // glides across instead of a bar that pretends to know.
                double phase = ((now - openSince) % 1.5) / 1.5;
                double width = track.Width * 0.32;
                double left = track.Left - width + (track.Width + width) * Ease(phase);
                float start = (float)Math.Max(track.Left, left);
                float end = (float)Math.Min(track.Right, left + width);
                fill = new RectangleF(start, track.Top, Math.Max(0, end - start), track.Height);
            }
            if (fill.Width < track.Height)
                return;
            using (GraphicsPath path = Pill(fill))
            using (LinearGradientBrush brush = new LinearGradientBrush(
                new RectangleF(fill.Left - 1, fill.Top, fill.Width + 2, fill.Height),
                Color.FromArgb(217, 54, 69), Color.FromArgb(255, 77, 79), LinearGradientMode.Horizontal))
                graphics.FillPath(brush, path);
        }

        void Present(byte opacity)
        {
            Native.POINT origin = new Native.POINT();
            Native.POINT place = new Native.POINT();
            place.X = Left;
            place.Y = Top;
            Native.SIZE size = new Native.SIZE();
            size.CX = pixelWidth;
            size.CY = pixelHeight;
            Native.BLENDFUNCTION blend = new Native.BLENDFUNCTION();
            blend.BlendOp = 0;                 // AC_SRC_OVER
            blend.SourceConstantAlpha = opacity;
            blend.AlphaFormat = 1;             // AC_SRC_ALPHA: premultiplied pixels
            Native.UpdateLayeredWindow(Handle, screen, ref place, ref size, memory, ref origin, 0, ref blend, 2);
        }
    }

    internal static class Program
    {
        const string AppIdentifier = "WhiteCritter.LuvBox";

        [STAThread]
        static int Main()
        {
            string arguments = ArgumentTail(Environment.CommandLine);
            bool interactive = arguments.Length == 0;
            // A scheduled save has no window either, and its failures are told
            // to the person. Diagnostic runs only return their code.
            bool speak = (interactive || arguments == "--run-saved" || arguments == "--run-saved --full")
                && Environment.GetEnvironmentVariable("LUVBOX_LAUNCHER_SILENT") != "1";
            try
            {
                string self = Process.GetCurrentProcess().MainModule.FileName;
                RemoveReplaced(self);
                Footer footer = Footer.Read(self);
                if (footer == null)
                    throw new LauncherError(ExitCodes.NoApplication, Messages.NoApplication);
                Installation installation = new Installation(self, footer);
                Environment.SetEnvironmentVariable("LUVBOX_LAUNCHER", self);
                try { Native.SetCurrentProcessExplicitAppUserModelID(AppIdentifier); }
                catch (Exception) { }

                if (!interactive)
                {
                    if (!installation.Ready())
                        installation.Prepare();
                    return RunAndWait(installation, arguments);
                }
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                Session session = new Session();
                session.Installation = installation;
                session.Arguments = arguments;
                Application.Run(new Splash(session));
                if (session.Error != null)
                    throw session.Error;
                return session.ExitCode;
            }
            catch (LauncherError error)
            {
                if (speak)
                    MessageBox.Show(error.Message, "LuvBox", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return error.Code;
            }
            catch (Exception)
            {
                if (speak)
                    MessageBox.Show(Messages.StartFailed, "LuvBox", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return ExitCodes.StartFailed;
            }
        }

        /// <summary>Everything after the program name, exactly as it was typed.</summary>
        static string ArgumentTail(string commandLine)
        {
            int index = 0;
            if (commandLine.Length > 0 && commandLine[0] == '"')
            {
                index = commandLine.IndexOf('"', 1);
                index = index < 0 ? commandLine.Length : index + 1;
            }
            else
            {
                while (index < commandLine.Length && commandLine[index] != ' ' && commandLine[index] != '\t')
                    index++;
            }
            return commandLine.Substring(index).Trim(' ', '\t');
        }

        /// <summary>An update leaves the program it replaced beside this one; remove it.</summary>
        static void RemoveReplaced(string self)
        {
            try
            {
                string folder = Path.GetDirectoryName(self);
                string name = Path.GetFileName(self);
                Regex shape = new Regex("^" + Regex.Escape(name) + "\\.old(\\.[0-9]{1,3})?$", RegexOptions.IgnoreCase);
                foreach (string candidate in Directory.GetFiles(folder, name + ".old*"))
                {
                    if (!shape.IsMatch(Path.GetFileName(candidate)))
                        continue;
                    try
                    {
                        File.SetAttributes(candidate, FileAttributes.Normal);
                        File.Delete(candidate);
                    }
                    catch (Exception) { }  // still running a scheduled save: next start
                }
            }
            catch (Exception) { }
        }

        static Process Start(Installation installation, string arguments)
        {
            ProcessStartInfo start = new ProcessStartInfo(installation.InnerPath, arguments);
            start.UseShellExecute = false;
            try
            {
                Process child = Process.Start(start);
                if (child == null)
                    throw new LauncherError(ExitCodes.StartFailed, Messages.StartFailed);
                return child;
            }
            catch (LauncherError) { throw; }
            catch (Exception)
            {
                throw new LauncherError(ExitCodes.StartFailed, Messages.StartFailed);
            }
        }

        /// <summary>Diagnostic and scheduled runs: wait, and end the application if this process is ended.</summary>
        static int RunAndWait(Installation installation, string arguments)
        {
            using (Process child = Start(installation, arguments))
            {
                try
                {
                    IntPtr job = Native.CreateJobObject(IntPtr.Zero, null);
                    if (job != IntPtr.Zero)
                    {
                        Native.JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = new Native.JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
                        limits.BasicLimitInformation.LimitFlags = 0x2000;  // end with the last handle to the job
                        if (Native.SetInformationJobObject(job, 9, ref limits, Marshal.SizeOf(limits)))
                            Native.AssignProcessToJobObject(job, child.Handle);
                    }
                }
                catch (Exception) { }
                child.WaitForExit();
                return child.ExitCode;
            }
        }

        /// <summary>The normal start: return once the application's window has drawn.</summary>
        public static int RunInteractive(Installation installation, string arguments)
        {
            string signal = "Local\\LuvBox-ready-" + Guid.NewGuid().ToString("N");
            using (EventWaitHandle ready = new EventWaitHandle(false, EventResetMode.ManualReset, signal))
            {
                Environment.SetEnvironmentVariable("LUVBOX_READY_EVENT", signal);
                using (Process child = Start(installation, arguments))
                {
                    try { Native.AllowSetForegroundWindow(child.Id); }
                    catch (Exception) { }
                    Stopwatch waited = Stopwatch.StartNew();
                    long windowSeen = -1;
                    while (true)
                    {
                        if (ready.WaitOne(100))
                            return 0;
                        if (child.HasExited)
                            return child.ExitCode;
                        // An application that never signals (an error box, an
                        // older build) still ends the splash soon after a window shows.
                        child.Refresh();
                        if (windowSeen < 0 && child.MainWindowHandle != IntPtr.Zero)
                            windowSeen = waited.ElapsedMilliseconds;
                        if (windowSeen >= 0 && waited.ElapsedMilliseconds - windowSeen > 2500)
                            return 0;
                        if (waited.ElapsedMilliseconds > 120000)
                            return 0;
                    }
                }
            }
        }
    }
}
