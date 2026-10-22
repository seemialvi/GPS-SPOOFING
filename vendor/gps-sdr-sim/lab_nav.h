/* Project LNAV paging extension. See IS-GPS-200, section 20.3.3.5.
 * Words here contain 24 data bits at bits 29..6. The upstream encoder
 * inserts HOW, parity and data inversion after selecting each page.
 */
static ephem_t labAlmanac[MAX_SAT];
static gpstime_t labToa;

static void labInitAlmanac(const ephem_t *eph, gpstime_t g)
{
    memcpy(labAlmanac, eph, sizeof(labAlmanac));
    labToa = g;
    labToa.sec = floor(g.sec / 4096.0) * 4096.0;
}

static unsigned long labQuant(double x, int power, int bits)
{
    return ((unsigned long)(long)round(ldexp(x, -power))) & ((1UL << bits)-1UL);
}

static double labAngle(double a)
{
    return a - 2.0*PI*floor((a+PI)/(2.0*PI));
}

/* Set a field in the parity-free 24-bit payload stream of words 3..10. */
static void labField(unsigned long *words, int offset, int width, unsigned long value)
{
    int b;
    for (b=0; b<width; b++) {
        int pos = offset+b;
        unsigned long mask = 1UL << (29-(pos%24));
        words[2+pos/24] &= ~mask;
        if ((value >> (width-1-b)) & 1UL)
            words[2+pos/24] |= mask;
    }
}

static unsigned long labHealth(int prn)
{
    return labAlmanac[prn-1].vflg ? labAlmanac[prn-1].svhlth & 63UL : 63UL;
}

static void labAlmanacPage(unsigned long *sbf, int prn)
{
    ephem_t e = labAlmanac[prn-1];
    double dt, dc, m0, omega, inc, af0;
    unsigned long clock0, clock1;
    int i;
    if (!e.vflg) {
        /* Dummy SV (ID zero), alternating data, valid parity added later. */
        for (i=2; i<10; i++) sbf[i] = 0xAAAAAAUL << 6;
        labField(sbf, 0, 8, 64UL);
        return;
    }
    dt = subGpsTime(labToa, e.toe);
    dc = subGpsTime(labToa, e.toc);
    m0 = labAngle(e.m0 + e.n*dt);
    omega = labAngle(e.omg0 + e.omgdot*dt);
    inc = e.inc0 + e.idot*dt;
    af0 = e.af0 + e.af1*dc + e.af2*dc*dc;
    labField(sbf, 0, 2, 1);
    labField(sbf, 2, 6, (unsigned long)prn);
    labField(sbf, 8, 16, labQuant(e.ecc, -21, 16));
    labField(sbf, 24, 8, (unsigned long)(labToa.sec/4096.0));
    labField(sbf, 32, 16, labQuant(inc/PI-0.30, -19, 16));
    labField(sbf, 48, 16, labQuant(e.omgdot/PI, -38, 16));
    labField(sbf, 64, 8, e.svhlth ? 0x80UL | (e.svhlth & 31UL) : 0UL);
    labField(sbf, 72, 24, labQuant(e.sqrta, -11, 24));
    labField(sbf, 96, 24, labQuant(omega/PI, -23, 24));
    labField(sbf, 120, 24, labQuant(e.aop/PI, -23, 24));
    labField(sbf, 144, 24, labQuant(m0/PI, -23, 24));
    clock0 = labQuant(af0, -20, 11);
    clock1 = labQuant(e.af1+2.0*e.af2*dc, -38, 11);
    labField(sbf, 168, 8, clock0 >> 3);
    labField(sbf, 176, 11, clock1);
    labField(sbf, 187, 3, clock0 & 7UL);
}

static void labPages(channel_t *chan, gpstime_t frame)
{
    static const int ids[25] = {
        57,25,26,27,28,57,29,30,31,32,57,62,52,53,54,57,55,56,58,59,57,60,61,62,63
    };
    unsigned long iono[10];
    int page = ((int)floor(frame.sec/30.0))%25;
    int id = ids[page];
    int i, sv;
    /* eph2sbf retains the iono/UTC page separately from the paged buffer. */
    memcpy(iono, chan->lab_iono, sizeof(iono));
    for (i=2; i<10; i++) chan->sbf[3][i] = chan->sbf[4][i] = 0UL;
    labField(chan->sbf[3], 0, 2, 1);
    labField(chan->sbf[3], 2, 6, (unsigned long)id);
    labField(chan->sbf[4], 0, 2, 1);
    if (id>=25 && id<=32) labAlmanacPage(chan->sbf[3], id);
    if (id==56) memcpy(chan->sbf[3], iono, sizeof(iono));
    if (id==52) labField(chan->sbf[3], 8, 2, 2); /* NMCT unavailable */
    if (id==55) {
        /* A blank 22-character ASCII special message, followed by reserved bits. */
        for (i=0; i<22; i++) labField(chan->sbf[3], 8+8*i, 8, 32);
    }
    if (id==63) {
        for (sv=1; sv<=32; sv++)
            labField(chan->sbf[3], 8+4*(sv-1), 4, labAlmanac[sv-1].vflg ? 1UL : 0UL);
        labField(chan->sbf[3], 138, 6, labHealth(25));
        for (sv=26; sv<=29; sv++) labField(chan->sbf[3], 144+6*(sv-26), 6, labHealth(sv));
        for (sv=30; sv<=32; sv++) labField(chan->sbf[3], 168+6*(sv-30), 6, labHealth(sv));
    }
    if (page<24) labAlmanacPage(chan->sbf[4], page+1);
    else {
        labField(chan->sbf[4], 2, 6, 51);
        labField(chan->sbf[4], 8, 8, (unsigned long)(labToa.sec/4096.0));
        labField(chan->sbf[4], 16, 8, (unsigned long)(labToa.week%256));
        for (sv=1; sv<=24; sv++) labField(chan->sbf[4], 24+6*(sv-1), 6, labHealth(sv));
    }
}
