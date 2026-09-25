using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace NextAI.Common
{
    public sealed class JObject : Dictionary<string, object>
    {
        public JObject() : base(StringComparer.Ordinal) { }

        public string Str(string key, string def = "")
        {
            object v;
            if (!TryGetValue(key, out v) || v == null) return def;
            if (v is double d) return d.ToString(CultureInfo.InvariantCulture);
            if (v is long l) return l.ToString(CultureInfo.InvariantCulture);
            if (v is bool b) return b ? "true" : "false";
            return v as string ?? Json.Serialize(v);
        }

        public double Num(string key, double def = 0)
        {
            object v;
            if (!TryGetValue(key, out v) || v == null) return def;
            if (v is long l) return l;
            if (v is double d) return d;
            double r;
            return double.TryParse(Convert.ToString(v, CultureInfo.InvariantCulture), NumberStyles.Float, CultureInfo.InvariantCulture, out r) ? r : def;
        }

        public long Int(string key, long def = 0) => (long)Math.Round(Num(key, def));

        public bool Bool(string key, bool def = false)
        {
            object v;
            if (!TryGetValue(key, out v) || v == null) return def;
            if (v is bool b) return b;
            if (v is long l) return l != 0;
            return def;
        }

        public JObject Obj(string key)
        {
            object v;
            return TryGetValue(key, out v) ? v as JObject ?? new JObject() : new JObject();
        }

        public JArray Arr(string key)
        {
            object v;
            return TryGetValue(key, out v) ? v as JArray ?? new JArray() : new JArray();
        }

        public bool IsNull(string key)
        {
            object v;
            return !TryGetValue(key, out v) || v == null;
        }
    }

    public sealed class JArray : List<object>
    {
        public IEnumerable<JObject> Objects()
        {
            foreach (var o in this) if (o is JObject j) yield return j;
        }
    }

    public static class Json
    {
        public static object Parse(string text)
        {
            var p = new Parser(text ?? "");
            p.Ws();
            var v = p.Value();
            p.Ws();
            if (!p.End) throw new FormatException("trailing characters in JSON");
            return v;
        }

        public static JObject ParseObject(string text) => Parse(text) as JObject ?? new JObject();

        public static string Serialize(object value)
        {
            var sb = new StringBuilder();
            Write(sb, value);
            return sb.ToString();
        }

        static void Write(StringBuilder sb, object v)
        {
            switch (v)
            {
                case null: sb.Append("null"); return;
                case string s: WriteString(sb, s); return;
                case bool b: sb.Append(b ? "true" : "false"); return;
                case double d:
                    if (double.IsNaN(d) || double.IsInfinity(d)) { sb.Append("null"); return; }
                    sb.Append(d.ToString("R", CultureInfo.InvariantCulture)); return;
                case float f: sb.Append(((double)f).ToString("R", CultureInfo.InvariantCulture)); return;
                case decimal m: sb.Append(m.ToString(CultureInfo.InvariantCulture)); return;
                case int _:
                case long _:
                case short _:
                case byte _:
                case uint _:
                case ulong _:
                    sb.Append(Convert.ToString(v, CultureInfo.InvariantCulture)); return;
                case IDictionary dict:
                    sb.Append('{');
                    var first = true;
                    foreach (DictionaryEntry e in dict)
                    {
                        if (!first) sb.Append(',');
                        first = false;
                        WriteString(sb, Convert.ToString(e.Key, CultureInfo.InvariantCulture));
                        sb.Append(':');
                        Write(sb, e.Value);
                    }
                    sb.Append('}');
                    return;
                case IEnumerable list:
                    sb.Append('[');
                    var f2 = true;
                    foreach (var item in list)
                    {
                        if (!f2) sb.Append(',');
                        f2 = false;
                        Write(sb, item);
                    }
                    sb.Append(']');
                    return;
                default:
                    WriteString(sb, v.ToString()); return;
            }
        }

        static void WriteString(StringBuilder sb, string s)
        {
            sb.Append('"');
            foreach (var c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    case '\b': sb.Append("\\b"); break;
                    case '\f': sb.Append("\\f"); break;
                    default:
                        if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
                        else sb.Append(c);
                        break;
                }
            }
            sb.Append('"');
        }

        sealed class Parser
        {
            readonly string s;
            int i;
            int depth;
            public Parser(string text) { s = text; }
            public bool End => i >= s.Length;

            public void Ws()
            {
                while (i < s.Length && (s[i] == ' ' || s[i] == '\t' || s[i] == '\n' || s[i] == '\r')) i++;
            }

            char Peek() => i < s.Length ? s[i] : '\0';

            void Expect(char c)
            {
                if (Peek() != c) throw new FormatException($"expected '{c}' at {i}");
                i++;
            }

            public object Value()
            {
                if (++depth > 256) throw new FormatException("JSON nesting too deep");
                try
                {
                    Ws();
                    var c = Peek();
                    if (c == '{') return ObjectValue();
                    if (c == '[') return ArrayValue();
                    if (c == '"') return StringValue();
                    if (c == 't') { Literal("true"); return true; }
                    if (c == 'f') { Literal("false"); return false; }
                    if (c == 'n') { Literal("null"); return null; }
                    return NumberValue();
                }
                finally { depth--; }
            }

            void Literal(string lit)
            {
                if (string.CompareOrdinal(s, i, lit, 0, lit.Length) != 0) throw new FormatException($"invalid literal at {i}");
                i += lit.Length;
            }

            JObject ObjectValue()
            {
                Expect('{');
                var o = new JObject();
                Ws();
                if (Peek() == '}') { i++; return o; }
                while (true)
                {
                    Ws();
                    var k = StringValue();
                    Ws();
                    Expect(':');
                    o[k] = Value();
                    Ws();
                    if (Peek() == ',') { i++; continue; }
                    Expect('}');
                    return o;
                }
            }

            JArray ArrayValue()
            {
                Expect('[');
                var a = new JArray();
                Ws();
                if (Peek() == ']') { i++; return a; }
                while (true)
                {
                    a.Add(Value());
                    Ws();
                    if (Peek() == ',') { i++; continue; }
                    Expect(']');
                    return a;
                }
            }

            string StringValue()
            {
                Expect('"');
                var sb = new StringBuilder();
                while (true)
                {
                    if (i >= s.Length) throw new FormatException("unterminated string");
                    var c = s[i++];
                    if (c == '"') return sb.ToString();
                    if (c != '\\') { sb.Append(c); continue; }
                    if (i >= s.Length) throw new FormatException("bad escape");
                    var e = s[i++];
                    switch (e)
                    {
                        case '"': sb.Append('"'); break;
                        case '\\': sb.Append('\\'); break;
                        case '/': sb.Append('/'); break;
                        case 'b': sb.Append('\b'); break;
                        case 'f': sb.Append('\f'); break;
                        case 'n': sb.Append('\n'); break;
                        case 'r': sb.Append('\r'); break;
                        case 't': sb.Append('\t'); break;
                        case 'u':
                            if (i + 4 > s.Length) throw new FormatException("bad unicode escape");
                            sb.Append((char)Convert.ToInt32(s.Substring(i, 4), 16));
                            i += 4;
                            break;
                        default: throw new FormatException("bad escape");
                    }
                }
            }

            object NumberValue()
            {
                var start = i;
                if (Peek() == '-') i++;
                while (i < s.Length && (char.IsDigit(s[i]) || s[i] == '.' || s[i] == 'e' || s[i] == 'E' || s[i] == '+' || s[i] == '-')) i++;
                var tok = s.Substring(start, i - start);
                if (tok.Length == 0) throw new FormatException($"unexpected character at {start}");
                long l;
                if (tok.IndexOfAny(new[] { '.', 'e', 'E' }) < 0 && long.TryParse(tok, NumberStyles.Integer, CultureInfo.InvariantCulture, out l)) return l;
                double d;
                if (double.TryParse(tok, NumberStyles.Float, CultureInfo.InvariantCulture, out d)) return d;
                throw new FormatException($"bad number '{tok}'");
            }
        }
    }
}
