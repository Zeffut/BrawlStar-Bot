import sys
import platform
import subprocess
import os

# Fixes missing setuptools
def bootstrap():
    if os.environ.get("PYLAAI_BOOTSTRAP") == "1": return
    try:
        import setuptools
    except ImportError:
        print("\nDetected missing core tools. Stabilizing environment...")
        os.environ["PYLAAI_BOOTSTRAP"] = "1"
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"])
        subprocess.run([sys.executable] + sys.argv)
        sys.exit(0) 

if any(cmd in sys.argv for cmd in ["install", "develop"]):
    bootstrap()

from setuptools import setup, find_packages

def force_install(reqs, no_deps=False):
    cmd = [sys.executable, "-m", "pip", "install"]
    if no_deps: cmd += ["--force-reinstall", "--no-deps"]
    subprocess.check_call(cmd + reqs)

def get_gpu_data():
    """Detects exact NVIDIA/AMD/Intel architecture."""
    #  NVIDIA cards Check
    try:
        output = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,compute_cap", "--format=csv,noheader,nounits"],
            encoding='utf-8', stderr=subprocess.DEVNULL).strip()
        name, cc = output.split(', ')
        return "nvidia", float(cc), name
    except: pass

    # AMD/Intel cards Check (Windows)
    if platform.system() == "Windows":
        try:
            wmic = subprocess.check_output(["wmic", "path", "win32_VideoController", "get", "name"], encoding='utf-8')
            if "Intel" in wmic: return "intel", 0.0, "Intel HD/Arc Graphics"
            if "AMD" in wmic or "Radeon" in wmic: return "amd_windows", 0.0, "AMD Radeon"
        except: pass

    # AMD/Intel cards Check (Linux Only)
    if platform.system() == "Linux":
        try:
            lspci = subprocess.check_output(["lspci"], encoding='utf-8')
            if "Intel" in lspci: return "intel", 0.0, "Intel Graphics (OpenVINO)"
            if os.path.exists("/dev/kfd"):
                roc_info = subprocess.check_output(["rocminfo"], encoding='utf-8', stderr=subprocess.DEVNULL)
                if "gfx12" in roc_info or "gfx11" in roc_info: return "amd_modern", 11.0, "AMD RDNA 3/4"
                return "amd_legacy", 6.0, "AMD RDNA 1/2"
        except: pass

    # Apple Check
    if platform.system() == "Darwin":
        try:
            cpu_info = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"]).decode()
            if "Apple" in cpu_info: return "apple", 0.0, "Apple Silicon"
        except: pass

    return "cpu", 0.0, "Generic CPU"

def ask_user(prompt_text):
    print(f"\n{prompt_text} (Y/N): ", end='', flush=True)
    response = sys.stdin.readline().strip().lower()
    return response in ['y', 'yes']

def setup_pyla():
    print("\n" + "="*50 + "\n   PylaAi - Setup   \n" + "="*50)
    
    # installing must have Pytorch CPU
    force_install(["torch", "torchvision", "--index-url", "https://download.pytorch.org/whl/cpu"])

    #  installing some must have dependencies
    print("Installing Core Dependencies...")
    base_reqs = [
        "customtkinter>=5.2.0", "toml>=0.10.2", "Pillow>=10.0.0", "discord.py>=2.3.2",
        "opencv-python==4.8.0.76", "requests", "ultralytics", "aiohttp", "easyocr", 
        "google-play-scraper",
        "onnxruntime"
    ]
    force_install(base_reqs)

    target, ver, name = get_gpu_data()
    status_pytorch, status_accel = "CPU Edition", "N/A"

    # --- THE CHOICE BRANCHES ---
    
    # NVIDIA BRANCH (Series 10-50)
    if target == "nvidia":
        print(f"\n NVIDIA: {name} detected.")
        if ask_user("Install NVIDIA CUDA acceleration? (takes more storge but gives you more ips about 2gb)"):
            if ver >= 10.0: # 50-Series Blackwell
                torch_cmd = ["--pre", "torch", "torchvision", "--index-url", "https://download.pytorch.org/whl/nightly/cu128"]
                status_accel = "CUDA 12.8 (Blackwell)"
            elif ver >= 8.9: # 40-Series Ada
                torch_cmd = ["torch", "torchvision", "--index-url", "https://download.pytorch.org/whl/cu124"]
                status_accel = "CUDA 12.4 (Ada)"
            else: # 10/20/30-Series
                torch_cmd = ["torch", "torchvision", "--index-url", "https://download.pytorch.org/whl/cu121"]
                status_accel = "CUDA 12.1 (Standard)"
            
            force_install(torch_cmd)
            status_pytorch = "CUDA Edition"

    # INTEL BRANCH (OpenVINO)
    elif target == "intel":
        print(f"\n Intel: {name} detected.")
        if ask_user("Install Intel OpenVINO acceleration? (best for Intel Arc/Integrated GPUs)"):
            force_install(["onnxruntime-openvino"])
            status_pytorch = "OpenVINO Edition"
            status_accel = "OpenVINO"

    # AMD BRANCH (RDNA 1-4)
    elif "amd" in target:
        print(f"\n AMD: {name} detected.")
        if ask_user("Install AMD Hardware acceleration?"):
            if target == "amd_linux":
                torch_cmd = ["torch", "torchvision", "--index-url", "https://download.pytorch.org/whl/rocm6.0"]
                force_install(["onnxruntime-rocm"])
                status_pytorch = "ROCm Edition"
                status_accel = "ROCm 6.0"
            else:
                # Windows AMD users use DirectML
                force_install(["onnxruntime-directml"])
                status_accel = "DirectML"
            if "torch_cmd" in locals(): force_install(torch_cmd)

    # APPLE BRANCH
    elif target == "apple":
        print(f"\n Apple Silicon: {name} detected.")
        if ask_user("Install Apple Silicon (MPS) acceleration?"):
            force_install(["onnxruntime-silicon"])
            status_pytorch = "MPS/Metal Edition"
            status_accel = "CoreML/MPS"

    # some conflict fixes
    print("\n Finalizing and Repairing Conflicts...")
    force_install(["numpy<2.0.0"], no_deps=True)
    force_install(["adbutils==2.12.0", "av==12.3.0"])
    force_install(["git+https://github.com/leng-yue/py-scrcpy-client.git@v0.5.0"], no_deps=True)

    # the setup completes and give some info about what it did
    os.system('cls' if os.name == 'nt' else 'clear')
    print("="*50)
    print("            SETUP COMPLETED!")
    print("="*50)
    print(f"  - OS detected:      {platform.system()}")
    print(f"  - GPU Detected:     {name}")
    print(f"  - PyTorch:          {status_pytorch}")
    print(f"  - Accel Status:     {status_accel}")
    print("="*50 + "\n")

setup(
    name="PylaAI", version="1.0.0",
    packages=find_packages(exclude=["api", "cfg", "models", "typization"]),
    install_requires=[]
)

if any(cmd in sys.argv for cmd in ["install", "develop"]):
    try: setup_pyla()
    except Exception as e: print(f"\n[ERROR] {e}"); sys.exit(1)