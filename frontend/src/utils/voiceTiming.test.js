import api from '../api';
import * as voiceTiming from './voiceTiming';

jest.mock('../api', () => ({ get: jest.fn(), post: jest.fn() }));

describe('voiceTiming', () => {
  let clock;
  beforeEach(() => {
    clock = 1000;
    jest.spyOn(Date, 'now').mockImplementation(() => clock);
    api.get.mockReset();
    api.post.mockReset();
    // Server clock 5 s ahead; each round trip takes 40 ms.
    api.get.mockImplementation(async () => {
      clock += 20;
      const t = (clock + 5000) / 1000;
      clock += 20;
      return { data: { t } };
    });
    api.post.mockResolvedValue({ data: {} });
    jest.spyOn(console, 'info').mockImplementation(() => {});
  });
  afterEach(() => jest.restoreAllMocks());

  it('sends the turn once, keyed by its first reply node', async () => {
    voiceTiming.startTurn();                 // rec_stop at 1000
    clock = 3000;
    voiceTiming.setTurnNode(42);
    voiceTiming.setTurnNode(43);             // a continuation: ignored
    voiceTiming.mark('llm_node_known');
    clock = 9000;
    voiceTiming.mark('chunk_ready');
    voiceTiming.mark('chunk_ready');         // first mark counts
    clock = 9300;
    await voiceTiming.markPlaying();
    await voiceTiming.markPlaying();

    expect(api.post).toHaveBeenCalledTimes(1);
    const [url, body] = api.post.mock.calls[0];
    expect(url).toBe('/voice/timing');
    expect(body.node_id).toBe(42);
    expect(body.marks).toEqual({
      rec_stop: 1000, llm_node_known: 3000, chunk_ready: 9000, playing: 9300,
    });
    expect(body.offset_ms).toBe(5000);
    expect(body.rtt_ms).toBe(40);
  });

  it('ignores a chunk of another node, and a cancelled turn', async () => {
    voiceTiming.startTurn();
    voiceTiming.setTurnNode(42);
    voiceTiming.mark('chunk_ready', 99);     // not this turn's node
    await voiceTiming.markPlaying();
    expect(api.post).not.toHaveBeenCalled();
    voiceTiming.endTurn();
    voiceTiming.mark('chunk_ready', 42);
    await voiceTiming.markPlaying();
    expect(api.post).not.toHaveBeenCalled();
  });

  it('ignores playback that is not a voice turn’s first chunk', async () => {
    voiceTiming.startTurn();
    voiceTiming.setTurnNode(42);
    await voiceTiming.markPlaying();         // no chunk_ready yet
    expect(api.post).not.toHaveBeenCalled();
  });
});
