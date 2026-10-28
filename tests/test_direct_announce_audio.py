import array, tempfile, unittest, wave
from pathlib import Path
from unittest.mock import patch
import direct_announce as direct

class FakeEncoder:
    def encode_frame(self, pcm):
        assert len(pcm) == direct.FRAME_BYTES
        return b"\x11" * 30

def write_wav(path, data, rate=48000, channels=1, width=2):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels); w.setsampwidth(width); w.setframerate(rate); w.writeframes(data)

class WavInputTests(unittest.TestCase):
    def test_keeps_leading_trailing_silence_and_packet_size(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"sstv.wav"
            write_wav(p, array.array("h",[0]*960+[1200]*960+[0]*960).tobytes())
            with patch.object(direct,"OpusEncoder",FakeEncoder):
                packets, frames=direct.build_audio_wav(p)
            self.assertEqual(frames,3); self.assertEqual(len(packets[0]),185)
    def test_stereo_downmix_and_resample(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"stereo.wav"; data=array.array("h")
            for x in [1000,2000,3000]*800: data.extend((x,x))
            write_wav(p,data.tobytes(),rate=24000,channels=2)
            pcm,rate=direct.decode_pcm_wav(p)
            self.assertEqual(rate,24000); self.assertEqual(len(pcm),9600)
    def test_8bit_unsigned_conversion(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"8bit.wav"; write_wav(p,bytes([0,128,255])*1600,width=1)
            pcm,_=direct.decode_pcm_wav(p); samples=array.array("h"); samples.frombytes(pcm)
            self.assertEqual(samples[0],-32768); self.assertEqual(samples[1],0); self.assertGreater(samples[2],32000)
    def test_rejects_more_than_two_channels(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"surround.wav"; write_wav(p,b"\x00"*(960*3*2),channels=3)
            with self.assertRaisesRegex(ValueError,"声道数"): direct.decode_pcm_wav(p)
    def test_wav_dry_run_does_not_open_network(self):
        with patch("sys.argv",["direct_announce.py","--wav","input.wav","--dry-run"]), patch.object(direct,"build_audio_wav",return_value=([b"x"*185],1)), patch("builtins.print"):
            direct.main()

if __name__=="__main__": unittest.main(verbosity=2)
