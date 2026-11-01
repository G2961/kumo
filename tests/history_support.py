"""Compile and drive the production Kotlin/JGit history core without an Android build."""
import asyncio
import hashlib
import json
import os
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT.parent / '.local'
CACHE = LOCAL / 'gradle-cache/caches/modules-2/files-2.1'
LIBS = LOCAL / 'git-history-libs'
CLASSES = LOCAL / 'git-history-classes'
JAVA = 'D:/AndroidSDK/jdk-21.0.4+7/bin/java.exe'


def jar(group, artifact, version):
    cached = list((CACHE / group / artifact / version).glob('*/*.jar'))
    if cached:
        return cached[0]
    LIBS.mkdir(exist_ok=True)
    target = LIBS / f'{artifact}-{version}.jar'
    if not target.exists():
        url = f'https://repo.maven.apache.org/maven2/{group.replace(".", "/")}/{artifact}/{version}/{target.name}'
        data = urllib.request.urlopen(url, timeout=40).read()
        try:
            expected = urllib.request.urlopen(url + '.sha256', timeout=20).read().decode().strip().split()[0]
            actual = hashlib.sha256(data).hexdigest()
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            expected = urllib.request.urlopen(url + '.sha1', timeout=20).read().decode().strip().split()[0]
            actual = hashlib.sha1(data).hexdigest()
        assert actual == expected, 'Library checksum mismatch'
        target.write_bytes(data)
    return target


def compile_core():
    compiler = [jar('org.jetbrains.kotlin', 'kotlin-compiler-embeddable', '2.0.21'),
                jar('org.jetbrains.kotlin', 'kotlin-stdlib', '2.0.21'),
                jar('org.jetbrains.kotlin', 'kotlin-script-runtime', '2.0.21'),
                jar('org.jetbrains.kotlin', 'kotlin-reflect', '1.6.10'),
                jar('org.jetbrains.intellij.deps', 'trove4j', '1.0.20200330'),
                jar('org.jetbrains.kotlinx', 'kotlinx-coroutines-core-jvm', '1.6.4'),
                jar('org.jetbrains', 'annotations', '13.0')]
    libs = [jar('org.eclipse.jgit', 'org.eclipse.jgit', '6.10.0.202406032230-r'),
            jar('org.slf4j', 'slf4j-api', '2.0.13'),
            jar('com.googlecode.javaewah', 'JavaEWAH', '1.2.3'),
            jar('commons-codec', 'commons-codec', '1.17.0'),
            compiler[1], LOCAL / 'ssh-test-libs/json.jar']
    CLASSES.mkdir(exist_ok=True)
    cp = os.pathsep.join(map(str, libs))
    result = subprocess.run([JAVA, '-cp', os.pathsep.join(map(str, compiler)),
                             'org.jetbrains.kotlin.cli.jvm.K2JVMCompiler', '-no-stdlib', '-no-reflect',
                             '-jvm-target', '17', '-cp', cp, '-d', str(CLASSES),
                             str(ROOT / 'app/src/main/java/space/g2961/opencode/ChangeHistory.kt'),
                             str(ROOT / 'tests/ChangeHistoryHarness.kt')], capture_output=True, text=True)
    if result.returncode:
        # Compiler diagnostics contain source locations, never bridge payloads.
        raise RuntimeError(result.stderr)
    return str(CLASSES) + os.pathsep + cp


class HistoryCore:
    def __init__(self, process):
        self.process = process
        self.lock = asyncio.Lock()

    @classmethod
    async def start(cls, cp, directories):
        process = await asyncio.create_subprocess_exec(JAVA, '-cp', cp, 'ChangeHistoryHarnessKt',
                  *map(str, directories), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                  stderr=asyncio.subprocess.DEVNULL)
        return cls(process)

    async def request(self, action, directory, **data):
        async with self.lock:
            request = {'action': action, 'data': {'directory': str(directory), **data}}
            self.process.stdin.write((json.dumps(request) + '\n').encode())
            await self.process.stdin.drain()
            return json.loads(await self.process.stdout.readline())

    async def close(self):
        self.process.stdin.close()
        await self.process.wait()
