#!/usr/bin/env python3
"""
One-time diagnostic script to measure OpenAI TTS API response time
for a single 4096-char chunk. Run on production to understand baseline
latency before any streaming/SSE overhead.

Usage:
    python backend/scripts/benchmark_tts_first_chunk.py
"""

import sys
import os
import time
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from backend.app import create_app
from backend.utils.api_keys import get_openai_chat_key


# Sample text ~4000 chars (representative of a real chunk)
SAMPLE_TEXT = """
The history of artificial intelligence began in antiquity, with myths, stories
and rumors of artificial beings endowed with intelligence or consciousness by
master craftsmen. The seeds of modern AI were planted by philosophers who
attempted to describe the process of human thinking as the mechanical
manipulation of symbols. This work culminated in the invention of the
programmable digital computer in the 1940s, a machine based on the abstract
essence of mathematical reasoning. This device and the ideas behind it inspired
a handful of scientists to begin seriously discussing the possibility of
building an electronic brain.

The field of AI research was founded at a workshop held on the campus of
Dartmouth College, USA during the summer of 1956. Those who attended would
become the leaders of AI research for decades. Many of them predicted that a
machine as intelligent as a human being would exist in no more than a
generation, and they were given millions of dollars to make this vision come
true. Eventually, it became obvious that commercial developers and researchers
had grossly underestimated the difficulty of the project.

In 1973, in response to the criticism from James Lighthill and ongoing pressure
from congress, the U.S. and British Governments stopped funding undirected
research into artificial intelligence, and the difficult years that followed
became known as an AI winter. Seven years later, a visionary initiative by the
Japanese Government inspired governments and industry to provide AI with
billions of dollars, but by the late 1980s the investors became disillusioned
and withdrew funding again.

Investment and interest in AI boomed in the first decades of the 21st century
when machine learning was successfully applied to many problems in academia and
industry due to new methods, the application of powerful computer hardware, and
the collection of immense data sets. The field of deep learning emerged as a
dominant force around 2012, particularly with breakthroughs in image recognition
and natural language processing. Convolutional neural networks revolutionized
computer vision tasks, while recurrent neural networks and later transformer
architectures transformed how machines process sequential data.

By 2020, large language models like GPT-3 demonstrated remarkable capabilities
in generating human-like text, answering questions, and performing various
language tasks. These models, trained on vast corpora of text data, showed
emergent abilities that surprised even their creators. The release of ChatGPT
in late 2022 brought AI capabilities to the mainstream public, sparking
widespread discussion about the implications of artificial intelligence for
society, employment, education, and creative endeavors.

The rapid advancement of AI technology has raised important questions about
safety, alignment, and governance. Researchers and policymakers around the
world are grappling with how to ensure that increasingly powerful AI systems
remain beneficial and aligned with human values. The development of
constitutional AI, reinforcement learning from human feedback, and other
alignment techniques represents ongoing efforts to address these challenges.

Meanwhile, the application of AI has expanded into virtually every domain of
human activity. In healthcare, AI systems assist with diagnosis, drug discovery,
and personalized treatment plans. In science, AI accelerates research by
analyzing complex datasets and generating hypotheses. In the arts, AI tools
enable new forms of creative expression while also raising questions about
authorship and originality. The economic impact of AI continues to grow, with
both opportunities and challenges for workers and businesses worldwide.

As we look toward the future, the trajectory of AI development remains
uncertain but promising. Some researchers predict that artificial general
intelligence, capable of matching or exceeding human cognitive abilities across
all domains, may be achieved within the coming decades. Others caution that
significant technical and theoretical hurdles remain. What is clear is that
artificial intelligence will continue to shape our world in profound ways,
requiring thoughtful engagement from technologists, policymakers, and citizens
alike to ensure its benefits are widely shared.
""".strip()


def benchmark():
    app = create_app()

    with app.app_context():
        api_key = get_openai_chat_key(app.config)
        if not api_key:
            print("ERROR: No OpenAI API key configured")
            sys.exit(1)

        from openai import OpenAI
        client = OpenAI(api_key=api_key)

        text = SAMPLE_TEXT
        print(f"Text length: {len(text)} chars")
        print(f"Model: gpt-4o-mini-tts")
        print(f"Voice: alloy")
        print()

        # Measure time to first byte and total time
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            tmp_path = f.name

        print("Starting TTS request...")
        t_start = time.time()

        with client.audio.speech.with_streaming_response.create(
            model="gpt-4o-mini-tts",
            input=text,
            voice="alloy"
        ) as resp:
            t_first_byte = time.time()
            print(f"Time to first byte: {t_first_byte - t_start:.2f}s")
            resp.stream_to_file(tmp_path)

        t_done = time.time()

        # Get file size and duration
        file_size = os.path.getsize(tmp_path)
        from pydub import AudioSegment
        segment = AudioSegment.from_file(tmp_path, format="mp3")
        duration = len(segment) / 1000.0

        print(f"Time to complete download: {t_done - t_start:.2f}s")
        print(f"File size: {file_size / 1024:.1f} KB")
        print(f"Audio duration: {duration:.1f}s")
        print()

        # Run a second time to see if there's variance
        print("Running again to check variance...")
        t_start2 = time.time()

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            tmp_path2 = f.name

        with client.audio.speech.with_streaming_response.create(
            model="gpt-4o-mini-tts",
            input=text,
            voice="alloy"
        ) as resp:
            t_first_byte2 = time.time()
            print(f"Time to first byte: {t_first_byte2 - t_start2:.2f}s")
            resp.stream_to_file(tmp_path2)

        t_done2 = time.time()
        print(f"Time to complete download: {t_done2 - t_start2:.2f}s")

        # Cleanup
        os.unlink(tmp_path)
        os.unlink(tmp_path2)

        print()
        print("Summary:")
        print(f"  Run 1: {t_first_byte - t_start:.2f}s to first byte, {t_done - t_start:.2f}s total")
        print(f"  Run 2: {t_first_byte2 - t_start2:.2f}s to first byte, {t_done2 - t_start2:.2f}s total")


if __name__ == '__main__':
    benchmark()
