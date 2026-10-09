import subprocess
from pathlib import Path


def run_ffmpeg(input_path: str, output_path: str):
    """
    Executa o FFmpeg sobre um arquivo de vídeo.

    Nesta primeira versão fazemos apenas uma cópia/reencodificação
    simples para provar que o Worker consegue processar vídeo.
    """

    input_file = Path(input_path)
    output_file = Path(output_path)

    if not input_file.exists():
        raise FileNotFoundError(
            f"Arquivo de entrada não encontrado: {input_file}"
        )

    output_file.parent.mkdir(parents=True, exist_ok=True)

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_file),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        str(output_file),
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"FFmpeg falhou:\n{result.stderr}"
        )

    if not output_file.exists():
        raise RuntimeError(
            "FFmpeg terminou sem gerar o arquivo de saída."
        )

    return str(output_file)
