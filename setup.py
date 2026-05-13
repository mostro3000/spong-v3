from setuptools import setup, find_packages

setup(
    name="spong",
    version="3.5.11",
    description="SPONG - Simple System/Network Monitoring (Python 3 rewrite)",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        "pyyaml",
        "flask",
    ],
    entry_points={
        "console_scripts": [
            "spong=spong.status_sender:query_server",
            "spong-server=spong.server:main",
            "spong-client=spong.client_agent:main",
            "spong-network=spong.network_agent:main",
            "spong-message=spong.messenger:main",
            "spong-cleanup=spong.cleanup:main",
        ],
    },
)
