"""Run only the first edit of the offline living-room pilot."""

if __package__:
    from .pilot_abc import main as run_pilot
else:
    from pilot_abc import main as run_pilot


def main():
    run_pilot(only_a=True)


if __name__ == "__main__":
    main()
