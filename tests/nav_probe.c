/* Exercise the actual generator's LNAV encoder without writing RF samples. */
#define main generator_main
#include "../vendor/gps-sdr-sim/gpssim.c"
#undef main

int main(int argc, char **argv)
{
    ephem_t e[EPHEM_ARRAY_SIZE][MAX_SAT] = {0};
    ionoutc_t ion = {0};
    channel_t channels[6] = {0};
    gpstime_t g;
    int n, sv, count=0, frame, ch, word;
    if (argc!=2) return 2;
    ion.enable = TRUE;
    n = readRinexNavAll(e, &ion, argv[1]);
    if (n<1 || !ion.vflg) return 3;
    for (sv=0; sv<MAX_SAT && count<6; sv++) {
        if (e[0][sv].vflg && e[0][sv].svhlth==0) {
            channels[count].prn = sv+1;
            eph2sbf(e[0][sv], ion, channels[count].sbf);
            memcpy(channels[count].lab_iono, channels[count].sbf[3], sizeof(channels[count].lab_iono));
            g = e[0][sv].toc;
            count++;
        }
    }
    if (count!=6) return 4;
    g.sec = floor(g.sec/750.0)*750.0;
    labInitAlmanac(e[0], g);
    for (frame=0; frame<26; frame++) {
        for (ch=0; ch<6; ch++) {
            generateNavMsg(g, &channels[ch], frame==0);
            printf("%d %d %.0f", frame, channels[ch].prn, g.sec);
            /* Include the previous subframe 5 to verify word chaining. */
            for (word=0; word<N_DWRD; word++) printf(" %08lX", channels[ch].dwrd[word]);
            printf("\n");
        }
        g = incGpsTime(g, 30.0);
    }
    return 0;
}
