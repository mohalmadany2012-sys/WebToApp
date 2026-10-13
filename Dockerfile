FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# 1. تحديث النظام وتثبيت المتطلبات الأساسية وبايثون والجافا
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    openjdk-17-jdk \
    wget \
    unzip \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# 2. ضبط مسارات Java و Android في بيئة النظام
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV ANDROID_HOME=/opt/android-sdk
ENV PATH=$PATH:$JAVA_HOME/bin:$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools

# 3. تحميل وتثبيت أدوات أندرويد Command-line Tools الرسمية
RUN mkdir -p ${ANDROID_HOME}/cmdline-tools && \
    wget -q https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip -O /tmp/cmdline-tools.zip && \
    unzip -q /tmp/cmdline-tools.zip -d /tmp && \
    mv /tmp/cmdline-tools ${ANDROID_HOME}/cmdline-tools/latest && \
    rm /tmp/cmdline-tools.zip

# 4. قبول تراخيص جوجل وتثبيت حزم الـ SDK والـ Build-Tools المطلوبة
RUN yes | sdkmanager --licenses && \
    sdkmanager "platforms;android-34" "platforms;android-36" "build-tools;34.0.0"

# 5. تحديد مجلد العمل ونقل ملفات المشروع
WORKDIR /app
COPY requirements.txt /app/
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . /app

# 6. إعطاء صلاحيات التشغيل
RUN chmod -R 777 /app

EXPOSE 8000

CMD ["python3", "main.py"]