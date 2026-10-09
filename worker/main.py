import time


def main():
    print("🚀 Fábrica Video Worker iniciado")
    print("⏳ Aguardando jobs...")

    while True:
        # Por enquanto, apenas mantém o Worker vivo.
        # Na próxima etapa vamos conectar ao Supabase.
        time.sleep(10)


if __name__ == "__main__":
    main()
