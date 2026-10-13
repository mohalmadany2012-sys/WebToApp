import glob
import http.server
import io
import json
import os
from pathlib import Path
import random
import re
import shutil
import smtplib
import subprocess
import urllib.parse
import zipfile
from email.mime.text import MIMEText
from email.parser import BytesParser
from email.policy import default

try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

BASE_DIR = Path(__file__).resolve().parent
ANDROID_DIR = BASE_DIR / "android_project"
PREVIEW_DIR = BASE_DIR / "preview_files"
EXTRACTED_DIR = PREVIEW_DIR / "extracted"
AAB_OUTPUT = BASE_DIR / "AAB_OUTPUT"
GRADLE_TOOL_DIR = BASE_DIR / "gradle_tool"
TEMPLATES_DIR = BASE_DIR / "templates"
CONFIG_FILE = BASE_DIR / "app_config.json"
DEFAULT_KEYSTORE = BASE_DIR / "default_release.keystore"
CUSTOM_KEYSTORE = BASE_DIR / "uploaded_custom.keystore"
RECOVERY_FILE = BASE_DIR / "recovery_codes.json"

# تحميل المتغيرات من ملف .env السري محلياً إن وُجد
env_path = BASE_DIR / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.strip().split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

SMTP_SENDER_EMAIL = os.environ.get("SMTP_SENDER_EMAIL")
SMTP_APP_PASSWORD = os.environ.get("SMTP_APP_PASSWORD")

for folder in [PREVIEW_DIR, EXTRACTED_DIR, AAB_OUTPUT, TEMPLATES_DIR]:
    folder.mkdir(parents=True, exist_ok=True)


def process_user_icon_strictly(icon_bytes):
    if not icon_bytes:
        return

    res_dir = ANDROID_DIR / "app" / "src" / "main" / "res"
    drawable_dir = res_dir / "drawable"
    drawable_dir.mkdir(parents=True, exist_ok=True)
    target_file = drawable_dir / "app_icon.png"

    if HAS_PILLOW:
        try:
            img = Image.open(io.BytesIO(icon_bytes))
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            img.save(target_file, format="PNG")

            sizes = {
                "mipmap-mdpi": 48,
                "mipmap-hdpi": 72,
                "mipmap-xhdpi": 96,
                "mipmap-xxhdpi": 144,
                "mipmap-xxxhdpi": 192,
            }
            for folder_name, sz in sizes.items():
                m_dir = res_dir / folder_name
                m_dir.mkdir(parents=True, exist_ok=True)
                resized = img.resize((sz, sz), Image.Resampling.LANCZOS)
                resized.save(m_dir / "app_icon.png", format="PNG")
            return
        except Exception as e:
            print(f"[!] خطأ أثناء معالجة صورة الأيقونة: {e}")

    if icon_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        target_file.write_bytes(icon_bytes)
    else:
        raise ValueError("صيغة الأيقونة غير صالحة! يرجى رفع ملف صورة سليم بصيغة PNG أو JPG.")


def get_modern_java():
    java_home_env = os.environ.get("JAVA_HOME")
    if java_home_env:
        jp = Path(java_home_env)
        if (jp / "bin" / "java").exists() or (jp / "bin" / "java.exe").exists():
            return jp

    linux_paths = [
        Path("/usr/lib/jvm/java-17-openjdk-amd64"),
        Path("/usr/lib/jvm/java-21-openjdk-amd64"),
        Path("/opt/java/openjdk"),
    ]
    for lp in linux_paths:
        if (lp / "bin" / "java").exists():
            return lp

    username = os.environ.get("USERNAME", "USER")
    win_paths = [
        r"C:\Program Files\Eclipse Adoptium\jdk-17*",
        r"C:\Program Files\Eclipse Adoptium\jdk-21*",
        r"C:\Program Files\Android\Android Studio\jbr",
        rf"C:\Users\{username}\AppData\Local\Programs\Android Studio\jbr",
        rf"C:\Users\{username}\AppData\Local\JetBrains\Toolbox\apps\AndroidStudio\ch-0\jbr",
        r"C:\Program Files\Java\jdk-21*",
        r"C:\Program Files\Java\jdk-17*",
        r"C:\Program Files\Microsoft\jdk-17*",
        r"C:\Program Files\Microsoft\jdk-21*",
        r"C:\Program Files\Zulu\zulu-17*",
        r"C:\Program Files\Amazon Corretto\jdk17*",
    ]
    for pattern in win_paths:
        for match in glob.glob(pattern):
            if (Path(match) / "bin" / "java.exe").exists():
                return Path(match)

    return None


def get_local_gradle_executable():
    if os.name == "nt":
        if GRADLE_TOOL_DIR.exists():
            for root, _, files in os.walk(GRADLE_TOOL_DIR):
                if "gradle.bat" in files:
                    return os.path.join(root, "gradle.bat")
        return "gradle.bat"
    else:
        if GRADLE_TOOL_DIR.exists():
            for root, _, files in os.walk(GRADLE_TOOL_DIR):
                if "gradle" in files:
                    p = os.path.join(root, "gradle")
                    os.chmod(p, 0o755)
                    return p
        return "gradle"


def ensure_default_keystore(java_home):
    if DEFAULT_KEYSTORE.exists():
        return DEFAULT_KEYSTORE

    keytool_bin = java_home / "bin" / ("keytool.exe" if os.name == "nt" else "keytool")
    if not keytool_bin.exists():
        keytool_bin = "keytool"

    cmd = (
        f'"{keytool_bin}" -genkey -v -keystore "{DEFAULT_KEYSTORE}" '
        f'-alias releasekey -keyalg RSA -keysize 2048 -validity 10000 '
        f'-storepass 12345678 -keypass 12345678 '
        f'-dname "CN=Developer, OU=Mobile, O=Apps, L=Cairo, ST=Cairo, C=EG"'
    )
    subprocess.run(cmd, shell=True, capture_output=True)
    return DEFAULT_KEYSTORE


def sanitize_package_name(pkg):
    pkg = re.sub(r"[^a-zA-Z0-9_.]", "", pkg).strip(".")
    parts = [p for p in pkg.split(".") if p]
    if len(parts) < 2:
        return "com.webapp.builder"
    valid_parts = []
    for p in parts:
        if p[0].isdigit():
            p = "app_" + p
        valid_parts.append(p)
    return ".".join(valid_parts)


def build_output_zip_bundle(cfg, final_aab_path, active_keystore_path, ks_alias, ks_pass):
    zip_bundle_name = f"{cfg['app_name']}_v{cfg['version_name']}_Bundle.zip"
    zip_bundle_path = AAB_OUTPUT / zip_bundle_name

    cert_text = f"""---------------------------------------------------
بيانات اعتماد وشهادة توقيع التطبيق (Certification Details)
تطبيق: {cfg["app_name"]}
---------------------------------------------------

اسم التطبيق (App Name):         {cfg["app_name"]}
معرف الحزمة (Package ID):       {cfg["package_name"]}
اسم المفتاح المستعار (Alias):    {ks_alias}
كلمة سر المفتاح (Password):      {ks_pass}
اسم الإصدار (Version Name):     {cfg["version_name"]}
رمز الإصدار (Version Code):     {cfg["version_code"]}

---------------------------------------------------
تنبيه هام: احتفظ بملف المفتاح (.keystore/.jks) وكلمة السر!
ستحتاج إلى نفس هذا المفتاح لرفع أي تحديث جديد على متجر Google Play.
---------------------------------------------------
"""

    readme_text = f"""تهانينا! تم بناء حزمة تطبيق أندرويد لتطبيقك ({cfg['app_name']}) بنجاح.

محتويات الحزمة:
1. {final_aab_path.name}: ملف حزمة التطبيق AAB الجاهز للرفع مباشرة إلى Google Play Console.
2. certification.txt: يحتوي على كلمات السر وبيانات الإصدار الخاصة بالتطبيق.
3. release_key.keystore: المفتاح الرقمي الأصلي الذي تم توقيع الحزمة به.

تأكد من عدم ضياع هذا الملف.
"""

    with zipfile.ZipFile(zip_bundle_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(final_aab_path, arcname=final_aab_path.name)
        zf.writestr("certification.txt", cert_text)
        zf.writestr("readme.txt", readme_text)
        if active_keystore_path.exists():
            zf.write(active_keystore_path, arcname="release_key.keystore")

    return zip_bundle_path


def generate_full_android_project(cfg, java_home):
    pkg = sanitize_package_name(cfg["package_name"])

    if cfg.get("keystore_type") == "custom" and CUSTOM_KEYSTORE.exists():
        keystore_path = CUSTOM_KEYSTORE
        ks_pass = cfg.get("ks_password", "12345678")
        ks_alias = cfg.get("ks_alias", "releasekey")
        ks_alias_pass = cfg.get("ks_alias_password", ks_pass)
    else:
        keystore_path = ensure_default_keystore(java_home)
        ks_pass = "12345678"
        ks_alias = "releasekey"
        ks_alias_pass = "12345678"

    keystore_clean_path = str(keystore_path).replace("\\", "/")

    app_dir = ANDROID_DIR / "app"
    main_dir = app_dir / "src" / "main"
    res_dir = main_dir / "res"
    values_dir = res_dir / "values"
    drawable_dir = res_dir / "drawable"
    assets_dir = main_dir / "assets"

    pkg_path = Path(*pkg.split("."))
    java_dir = main_dir / "java" / pkg_path

    old_java_root = main_dir / "java"
    if old_java_root.exists():
        shutil.rmtree(old_java_root)

    for d in [values_dir, drawable_dir, assets_dir, java_dir]:
        d.mkdir(parents=True, exist_ok=True)

    (ANDROID_DIR / "build.gradle").write_text(
        """plugins {
    id 'com.android.application' version '8.2.2' apply false
}
""",
        encoding="utf-8",
    )

    (ANDROID_DIR / "settings.gradle").write_text(
        """pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}
rootProject.name = "WebToApp"
include(":app")
""",
        encoding="utf-8",
    )

    (app_dir / "proguard-rules.pro").write_text(
        """-keepattributes JavascriptInterface
-keepclassmembers class * {
    @android.webkit.JavascriptInterface <methods>;
}
-dontwarn android.webkit.**
""",
        encoding="utf-8",
    )

    (app_dir / "build.gradle").write_text(
        f"""plugins {{
    id 'com.android.application'
}}

android {{
    namespace '{pkg}'
    compileSdk 36

    aaptOptions {{
        cruncherEnabled = false
    }}

    defaultConfig {{
        applicationId "{pkg}"
        minSdk 24
        targetSdk 36
        versionCode {cfg["version_code"]}
        versionName "{cfg["version_name"]}"
    }}

    signingConfigs {{
        release {{
            storeFile file('{keystore_clean_path}')
            storePassword '{ks_pass}'
            keyAlias '{ks_alias}'
            keyPassword '{ks_alias_pass}'
        }}
    }}

    compileOptions {{
        sourceCompatibility JavaVersion.VERSION_17
        targetCompatibility JavaVersion.VERSION_17
    }}

    buildTypes {{
        release {{
            signingConfig signingConfigs.release
            minifyEnabled true
            shrinkResources true
            proguardFiles getDefaultProguardFile('proguard-android-optimize.txt'), 'proguard-rules.pro'
        }}
    }}
}}

dependencies {{
    implementation 'androidx.appcompat:appcompat:1.6.1'
    implementation 'androidx.webkit:webkit:1.10.0'
}}
""",
        encoding="utf-8",
    )

    (values_dir / "strings.xml").write_text(
        f"""<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="app_name">{cfg["app_name"]}</string>
</resources>
""",
        encoding="utf-8",
    )

    styles_file = values_dir / "styles.xml"
    if styles_file.exists():
        styles_file.unlink()

    (values_dir / "themes.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<resources>
    <style name="Theme.WebApp" parent="Theme.AppCompat.Light.NoActionBar">
        <item name="android:windowBackground">@android:color/white</item>
    </style>
</resources>
""",
        encoding="utf-8",
    )

    (main_dir / "AndroidManifest.xml").write_text(
        f"""<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">

    <uses-permission android:name="android.permission.INTERNET" />
    <uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />

    <application
        android:allowBackup="true"
        android:icon="@drawable/app_icon"
        android:label="@string/app_name"
        android:roundIcon="@drawable/app_icon"
        android:supportsRtl="true"
        android:theme="@style/Theme.WebApp">
        <activity
            android:name="{pkg}.MainActivity"
            android:exported="true"
            android:configChanges="orientation|screenSize|keyboardHidden">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>
    </application>

</manifest>
""",
        encoding="utf-8",
    )

    (java_dir / "MainActivity.java").write_text(
        f"""package {pkg};

import android.os.Bundle;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import androidx.activity.OnBackPressedCallback;
import androidx.appcompat.app.AppCompatActivity;

public class MainActivity extends AppCompatActivity {{

    private WebView webView;

    @Override
    protected void onCreate(Bundle savedInstanceState) {{
        super.onCreate(savedInstanceState);

        webView = new WebView(this);
        setContentView(webView);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(true);
        settings.setLoadsImagesAutomatically(true);

        webView.setWebViewClient(new WebViewClient());
        webView.loadUrl("file:///android_asset/index.html");

        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {{
            @Override
            public void handleOnBackPressed() {{
                if (webView.canGoBack()) {{
                    webView.goBack();
                }} else {{
                    setEnabled(false);
                    getOnBackPressedDispatcher().onBackPressed();
                }}
            }}
        }});
    }}
}}
""",
        encoding="utf-8",
    )

    return keystore_path, ks_alias, ks_pass


class BuilderHandler(http.server.SimpleHTTPRequestHandler):

    def do_GET(self):
        url_path = urllib.parse.urlparse(self.path).path
        if url_path in ["/", ""]:
            template = TEMPLATES_DIR / "index.html"
            if template.exists():
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(template.read_bytes())
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write("ملف templates/index.html غير موجود".encode("utf-8"))

        elif url_path.startswith("/preview_app"):
            html_files = list(EXTRACTED_DIR.rglob("index.html"))
            if html_files:
                target_index = html_files[0]
                req_file = url_path.replace("/preview_app", "").lstrip("/")
                target = target_index if not req_file else target_index.parent / req_file

                if target.exists() and target.is_file():
                    self.send_response(200)
                    mime = "text/plain"
                    if target.suffix == ".html":
                        mime = "text/html; charset=utf-8"
                    elif target.suffix == ".css":
                        mime = "text/css; charset=utf-8"
                    elif target.suffix == ".js":
                        mime = "application/javascript; charset=utf-8"
                    elif target.suffix in [".png", ".jpg", ".jpeg", ".webp"]:
                        mime = f"image/{target.suffix.replace('.', '')}"
                    self.send_header("Content-Type", mime)
                    self.end_headers()
                    self.wfile.write(target.read_bytes())
                    return
            self.send_response(404)
            self.end_headers()

        elif url_path == "/download":
            cfg = {}
            if CONFIG_FILE.exists():
                try:
                    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                except Exception:
                    pass
            app_name = cfg.get("app_name", "app-release")
            version_name = cfg.get("version_name", "1.0.0")

            target = AAB_OUTPUT / f"{app_name}_v{version_name}_Bundle.zip"
            if not target.exists():
                zips = list(AAB_OUTPUT.glob("*.zip"))
                target = zips[0] if zips else None

            if target and target.exists():
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                safe_name = urllib.parse.quote(target.name)
                self.send_header(
                    "Content-Disposition", f"attachment; filename*=UTF-8''{safe_name}"
                )
                self.end_headers()
                self.wfile.write(target.read_bytes())
            else:
                self.send_response(404)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        url_path = urllib.parse.urlparse(self.path).path

        if url_path == "/send_recovery_code":
            content_length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_length).decode("utf-8"))
            target_email = body.get("email", "").strip().lower()

            code = str(random.randint(100000, 999999))

            codes = {}
            if RECOVERY_FILE.exists():
                try:
                    codes = json.loads(RECOVERY_FILE.read_text(encoding="utf-8"))
                except Exception:
                    pass
            codes[target_email] = code
            RECOVERY_FILE.write_text(json.dumps(codes, ensure_ascii=False), encoding="utf-8")

            msg = MIMEText(
                f"مرحباً بك،\n\nكود التحقق الخاص بك لإعادة تعيين كلمة المرور في منصة تحويل الويب إلى تطبيق أندرويد هو:\n\n{code}\n\nصالح للاستخدام لمرة واحدة.",
                "plain",
                "utf-8",
            )
            msg["Subject"] = "كود التحقق لاستعادة الحساب"
            msg["From"] = SMTP_SENDER_EMAIL
            msg["To"] = target_email

            try:
                with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                    server.login(SMTP_SENDER_EMAIL, SMTP_APP_PASSWORD)
                    server.sendmail(SMTP_SENDER_EMAIL, target_email, msg.as_string())
                resp = {"status": "success", "message": "تم إرسال كود التحقق بنجاح إلى بريدك."}
            except Exception as e:
                resp = {"status": "error", "message": f"فشل إرسال البريد: {str(e)}"}

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return

        elif url_path == "/verify_and_reset_pass":
            content_length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_length).decode("utf-8"))
            target_email = body.get("email", "").strip().lower()
            code_entered = body.get("code", "").strip()

            codes = {}
            if RECOVERY_FILE.exists():
                try:
                    codes = json.loads(RECOVERY_FILE.read_text(encoding="utf-8"))
                except Exception:
                    pass

            if codes.get(target_email) == code_entered:
                del codes[target_email]
                RECOVERY_FILE.write_text(json.dumps(codes, ensure_ascii=False), encoding="utf-8")
                resp = {"status": "success", "message": "تم التحقق من الكود بنجاح!"}
            else:
                resp = {"status": "error", "message": "كود التحقق غير صحيح أو انتهت صلاحيته!"}

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return

        elif url_path == "/upload":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)

            header_bytes = f"Content-Type: {self.headers.get('Content-Type')}\r\n\r\n".encode("utf-8")
            msg = BytesParser(policy=default).parsebytes(header_bytes + body)

            form_values = {}
            icon_bytes = None
            zip_bytes = None
            keystore_bytes = None

            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                filename = part.get_filename()

                if filename:
                    if name == "icon_file":
                        icon_bytes = part.get_payload(decode=True)
                    elif name == "zip_file":
                        zip_bytes = part.get_payload(decode=True)
                    elif name == "keystore_file":
                        keystore_bytes = part.get_payload(decode=True)
                else:
                    if name:
                        payload = part.get_payload(decode=True)
                        if payload:
                            form_values[name] = payload.decode("utf-8", errors="replace").strip()

            app_name = form_values.get("app_name", "تطبيقي")
            package_name = form_values.get("package_name", "com.mycompany.app")
            version_name = form_values.get("version_name", "1.0.0")
            version_code = form_values.get("version_code", "1")
            keystore_type = form_values.get("keystore_type", "auto")

            if not version_code.isdigit():
                version_code = "1"

            config_data = {
                "app_name": app_name,
                "package_name": sanitize_package_name(package_name),
                "version_name": version_name,
                "version_code": int(version_code),
                "keystore_type": keystore_type,
                "ks_alias": form_values.get("ks_alias", "releasekey"),
                "ks_password": form_values.get("ks_password", "12345678"),
                "ks_alias_password": form_values.get("ks_alias_password", form_values.get("ks_password", "12345678")),
            }
            CONFIG_FILE.write_text(
                json.dumps(config_data, ensure_ascii=False), encoding="utf-8"
            )

            if keystore_type == "custom" and keystore_bytes and len(keystore_bytes) > 0:
                CUSTOM_KEYSTORE.write_bytes(keystore_bytes)

            if icon_bytes and len(icon_bytes) > 0:
                try:
                    process_user_icon_strictly(icon_bytes)
                except ValueError as ve:
                    resp = {"status": "error", "message": str(ve)}
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
                    return

            has_zip = False
            if zip_bytes and len(zip_bytes) > 0:
                if EXTRACTED_DIR.exists():
                    shutil.rmtree(EXTRACTED_DIR)
                EXTRACTED_DIR.mkdir(parents=True, exist_ok=True)

                zip_path = PREVIEW_DIR / "uploaded.zip"
                zip_path.write_bytes(zip_bytes)
                with zipfile.ZipFile(zip_path, "r") as zip_ref:
                    zip_ref.extractall(EXTRACTED_DIR)
                has_zip = True

            resp = {
                "status": "success",
                "message": "تم استلام البيانات وضبط الإعدادات بنجاح!",
                "has_preview": has_zip,
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))

        elif url_path == "/build":
            try:
                cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                java_home = get_modern_java()
                if not java_home:
                    raise RuntimeError("لم يتم العثور على JDK 17 أو 21.")

                gradle_executable = get_local_gradle_executable()
                if not gradle_executable:
                    raise RuntimeError("لم يتم العثور على أداة gradle للبناء.")

                active_ks_path, ks_alias, ks_pass = generate_full_android_project(cfg, java_home)

                assets_dest = ANDROID_DIR / "app" / "src" / "main" / "assets"
                if assets_dest.exists():
                    shutil.rmtree(assets_dest)
                assets_dest.parent.mkdir(parents=True, exist_ok=True)

                if EXTRACTED_DIR.exists():
                    html_files = list(EXTRACTED_DIR.rglob("index.html"))
                    if html_files:
                        source_folder = html_files[0].parent
                        shutil.copytree(source_folder, assets_dest)
                    else:
                        shutil.copytree(EXTRACTED_DIR, assets_dest)

                clean_path = str(java_home).replace("\\", "/")
                (ANDROID_DIR / "gradle.properties").write_text(
                    f"org.gradle.jvmargs=-Xmx2048m -Dfile.encoding=UTF-8\n"
                    f"android.useAndroidX=true\n"
                    f"org.gradle.java.home={clean_path}\n",
                    encoding="utf-8",
                )

                env_vars = os.environ.copy()
                env_vars["JAVA_HOME"] = str(java_home)
                env_vars["PATH"] = str(java_home / "bin") + os.pathsep + env_vars.get("PATH", "")
                env_vars["GRADLE_OPTS"] = "-Xmx2048m -Dorg.gradle.daemon=false"

                sdk_candidates = [
                    Path(os.environ.get("ANDROID_HOME", "")),
                    Path("/opt/android-sdk"),
                    Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk",
                    Path(r"C:\Android\Sdk"),
                    Path(r"C:\Users")
                    / os.environ.get("USERNAME", "")
                    / "AppData"
                    / "Local"
                    / "Android"
                    / "Sdk",
                ]
                for sdk in sdk_candidates:
                    if sdk and str(sdk) != "." and sdk.exists():
                        env_vars["ANDROID_HOME"] = str(sdk)
                        clean_sdk = str(sdk).replace("\\", "/")
                        (ANDROID_DIR / "local.properties").write_text(
                            f"sdk.dir={clean_sdk}\n",
                            encoding="utf-8",
                        )
                        break

                build_cmd = f'"{gradle_executable}" bundleRelease --no-daemon'
                process = subprocess.run(
                    build_cmd,
                    cwd=str(ANDROID_DIR),
                    env=env_vars,
                    shell=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )

                bundle_path = ANDROID_DIR / "app" / "build" / "outputs" / "bundle" / "release"
                aab_files = list(bundle_path.glob("*.aab")) if bundle_path.exists() else []

                if process.returncode == 0 and aab_files:
                    final_aab_name = f"{cfg['app_name']}.aab"
                    final_aab = AAB_OUTPUT / final_aab_name
                    shutil.copy2(aab_files[0], final_aab)

                    zip_bundle = build_output_zip_bundle(
                        cfg, final_aab, active_ks_path, ks_alias, ks_pass
                    )

                    resp = {
                        "status": "success",
                        "message": (
                            f"🎉 تم الانتهاء بنجاح! تم بناء حزمة التطبيق وتوقيعها بمستوى API 36.\n"
                            f"📦 تم إنشاء الحزمة المضغوطة الكاملة: {zip_bundle.name}"
                        ),
                        "download_url": "/download",
                    }
                else:
                    err = process.stderr if process.stderr else process.stdout
                    resp = {
                        "status": "error",
                        "message": f"خطأ أثناء بناء الحزمة:\n{err[-4000:]}",
                    }

            except Exception as e:
                resp = {"status": "error", "message": str(e)}

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print("=" * 60)
    print("منصة تحويل مواقع الويب إلى حزم تطبيقات أندرويد (إصدار متوافق مع متجر Google Play)")
    print(f"السيرفر يعمل الآن وجاهز للاستقبال على المنفذ: {port}")
    print("=" * 60)
    server = http.server.HTTPServer(("0.0.0.0", port), BuilderHandler)
    server.serve_forever()