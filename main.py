# -*- coding: utf-8 -*-
from datetime import datetime
from pprint import pprint
from typing import List
from collections import deque
import click
import csv
import logging
from cdr import Gcdr, Subscriber, Dvo, Interfacez, UserType, CallType

UNDEFINED_LOCATION: int = 65535
LOG = None  # initialized in init_logging

def bcdDigits(chars):
    for char in chars:
        char = ord(char)
        for val in (char >> 4, char & 0xF):
            if val == 0xF:
                return
            yield val

def bcd_to_str(arr: bytearray) -> str:
    return "".join([hex(i)[2:] for i in arr])

def bcd_to_time(tetra_time) -> datetime:
        return datetime(
            tetra_time.full_year,
            tetra_time.month.as_int,
            tetra_time.day.as_int,
            tetra_time.hour.as_int,
            tetra_time.min.as_int,
            tetra_time.sec.as_int,
            tetra_time.msec.as_int
        )
def to_sec(dec_msec: int) -> int:
    """
    Convert 10 msec unit from Tetra CDR to sec
    dec_msec: unit is 10 milliseconds
    """
    return round(dec_msec/100)

def init_logging(log_file=None, append=False, console_loglevel=logging.INFO):
    """Set up logging to file and console."""
    if log_file is not None:
        if append:
            filemode_val = 'a'
        else:
            filemode_val = 'w'
        logging.basicConfig(level=logging.DEBUG,
                            format="%(asctime)s %(levelname)s %(threadName)s %(name)s %(message)s",
                            # datefmt='%m-%d %H:%M',
                            filename=log_file,
                            filemode=filemode_val)
    # define a Handler which writes INFO messages or higher to the sys.stderr
    console = logging.StreamHandler()
    console.setLevel(console_loglevel)
    # set a format which is simpler for console use
    formatter = logging.Formatter("%(message)s")
    console.setFormatter(formatter)
    # add the handler to the root logger
    logging.getLogger('').addHandler(console)
    global LOG
    LOG = logging.getLogger(__name__)

def parseCDR(filename, version) -> List[Gcdr]:

    if version == 5:
        from kaitai.parser.tetra_v5 import Tetra
    else:
        from kaitai.parser.tetra_v7 import Tetra

    from sqlalchemy import create_engine
    engine = create_engine('sqlite:///test.db', echo=True)

    from sqlalchemy import Table, Column, Integer, String, DateTime, MetaData,    PrimaryKeyConstraint
    metadata = MetaData()
    regs = Table(
            'regs', metadata,
            Column('id', Integer, primary_key=True),
            Column('served_nitsi', String(12)),
            Column('location', Integer),
            Column('prev_location', Integer),
            Column('reg_at', DateTime),
            PrimaryKeyConstraint('id', 'served_nitsi', name='reg_pk')
            )
    metadata.create_all(engine)

    target = Tetra.from_file(filename)

    call_stack = deque()
    reg_buffer: List[Tetra.Reg] = []
    cdr_buffer: List[Gcdr] = []
    conn = engine.connect()

    for blk in target.block:
        LOG.info('Starting new block in CDR file')
        for event in blk.events.event:
            if event.body.type == Tetra.Types.toc:
                """ Обработка записи инициализации вызова TOC """
                if len(call_stack) != 0:
                   raise ValueError(f'Неожиданное вхождение записи TCC. Обработка звонка {event.body.call_reference} завершена не корректно.')
                LOG.debug(f'TOC: {event.body.seq_num} cr: {event.body.call_reference}')
                if event.body.members == 65535:
                    # Обработка персонального вызова
                    if event.body.call_reference == 0:
                        # Звонок не состоялся. Строим GCDR и сохраняем в CSV
                        toc = event.body
                        userA = Subscriber(UserType.inner, bcd_to_str(toc.served_number), toc.location, toc.location)
                        userB = Subscriber(UserType.inner, bcd_to_str(toc.called_number), UNDEFINED_LOCATION, UNDEFINED_LOCATION)
                        dvo = Dvo(False)
                        gdp = Gcdr(bcd_to_str(toc.dxt_id), 23, bcd_to_time(toc.setup_time),
                                    to_sec(toc.duration), userA, userB, None, None, toc.termination, dvo, CallType.toc)
                        cdr_buffer.append(gdp)
                    else:
                        # Звонок состоялся. Инициализируем GCDR и ждем TCC или OutG
                        call_stack.append(event.body)
                else:
                    # Обработка группового вызова. Строим GCDR и сохраняем его в CSV
                    toc = event.body
                    userA = Subscriber(UserType.inner, bcd_to_str(toc.served_number), toc.location, toc.location)
                    userB = Subscriber(UserType.inner, bcd_to_str(toc.called_number), UNDEFINED_LOCATION, UNDEFINED_LOCATION)
                    dvo = Dvo(False)
                    gdp = Gcdr(bcd_to_str(toc.dxt_id), 23, bcd_to_time(toc.setup_time),
                                to_sec(toc.duration), userA, userB, None, None, toc.termination, dvo, CallType.toc)
                    cdr_buffer.append(gdp)
            if event.body.type == Tetra.Types.tcc:
                """ Обработка запси терминации вызова TCC """
                if len(call_stack) == 0:
                    raise ValueError(f'Не обработана запис TOC или InG для звонка {event.body.call_reference}')
                LOG.debug(f'TCC: {event.body.seq_num} cr: {event.body.call_reference}')
                partial_cdr = call_stack.pop()
                if partial_cdr.call_reference == event.body.call_reference:
                    """Все совпало. Будем собирать Gcdr"""
                    tcc = event.body
                    dvo = Dvo(False)
                    if type(partial_cdr) is Tetra.Toc:
                        userA = Subscriber(UserType.inner, bcd_to_str(partial_cdr.served_number), partial_cdr.location, partial_cdr.location)
                        userB = Subscriber(UserType.inner, bcd_to_str(tcc.served_number), tcc.location, tcc.location)
                        gdp = Gcdr(bcd_to_str(partial_cdr.dxt_id), 23, bcd_to_time(partial_cdr.setup_time),
                                   to_sec(partial_cdr.duration), userA, userB, None, None, partial_cdr.termination, dvo, CallType.toctcc)
                        cdr_buffer.append(gdp)
                    elif type(partial_cdr) is Tetra.InG:
                        userA = Subscriber(UserType.outer, bcd_to_str(partial_cdr.calling_number), UNDEFINED_LOCATION, UNDEFINED_LOCATION)
                        userB = Subscriber(UserType.inner, bcd_to_str(tcc.served_nitsi), tcc.location, tcc.location)
                        gdp = Gcdr(bcd_to_str(tcc.dxt_id), 23, bcd_to_time(tcc.setup_time),
                                    to_sec(tcc.duration), userA, userB, None, None, tcc.termination, dvo, CallType.ingtcc)
                        cdr_buffer.append(gdp)
                    else:
                        raise ValueError(f'Неожиданный тип объекта {type(partial_cdr)}')
                else:
                    raise ValueError(f'Не соответствие call_reference обрабатываемых записей {partial_cdr.call_reference} != {event.body.call_reference}')
            if event.body.type == Tetra.Types.out_g:
                """ Обработка записи звонка исходящего на фиксированную сеть TOC -> OutG"""
                if len(call_stack) == 0:
                    raise ValueError(f'Не обработана запись TOC для звонка {event.body.call_reference}')
                LOG.debug(f'OutG: {event.body.seq_num} cr: {event.body.call_reference}')
                toc: Tetra.Toc = call_stack.pop()
                out_g: Tetra.OutG = event.body
                userA = Subscriber(UserType.inner, bcd_to_str(toc.served_number), toc.location, toc.location)
                userB = Subscriber(UserType.outer, bcd_to_str(out_g.transmitted_number), UNDEFINED_LOCATION, UNDEFINED_LOCATION)
                dvo = Dvo(False)
                gdp = Gcdr(bcd_to_str(toc.dxt_id), 23, bcd_to_time(toc.setup_time),
                            to_sec(toc.duration), userA, userB, None, Interfacez(out_g.out_int), toc.termination, dvo, CallType.tocoutg)
                cdr_buffer.append(gdp)
            if event.body.type == Tetra.Types.in_g:
                """ Обработка записи звонка пришедшего из внешней сети """
                if len(call_stack) != 0:
                    raise ValueError(f'Неожиданное вхождение записи IN_G. Обработка звонка {event.body.call_reference} завершена не корректно.')
                LOG.debug(f'InG: {event.body.seq_num} cr: {event.body.call_reference}')
                if event.body.call_reference == 0:
                    # Звонок не состоялся. Строим GCDR и сохраняем его в CSV
                    in_g: Tetra.InG = event.body
                    userA = Subscriber(UserType.outer, bcd_to_str(in_g.calling_number), UNDEFINED_LOCATION, UNDEFINED_LOCATION)
                    userB = Subscriber(UserType.inner, bcd_to_str(in_g.called_number), UNDEFINED_LOCATION, UNDEFINED_LOCATION)
                    dvo = Dvo(False)
                    gdp = Gcdr(bcd_to_str(in_g.dxt_id), 23, bcd_to_time(in_g.setup_time), to_sec(in_g.duration),
                               userA, userB, Interfacez(in_g.inc_int), None, in_g.termination, dvo, CallType.ing)
                    cdr_buffer.append(gdp)
                else:
                    # Продолжаем обрабатывать звонок
                    call_stack.append(event.body)

            if event.body.type == Tetra.Types.reg:
                """ Обработка записи о регистрации абонента """
                LOG.debug(f'REG: {event.body.seq_num} SERVED_NITSI: {bcd_to_str(event.body.served_nitsi)} LOCATION: {event.body.location}:{event.body.prev_location}')
                reg_buffer.append(
                    dict(
                        id = event.body.seq_num,
                        served_nitsi = bcd_to_str(event.body.served_nitsi),
                        location = event.body.location,
                        prev_location = event.body.prev_location,
                        reg_at = bcd_to_time(event.body.timestamp),
                    )
                )
        LOG.info(f'End reading block. Calls quantity: {len(cdr_buffer)}. Regs quantity: {len(reg_buffer)}')
        # Write REG records to BD
        if len(reg_buffer) > 0:
            conn.execute(regs.insert(), reg_buffer)
            reg_buffer.clear()
    return cdr_buffer


def write_to_csv(cdr_buffer: List[Gcdr], file: str):
    # Write gcdrs to file
    with open(file, 'a', newline='') as csv_file:
        wr = csv.writer(csv_file, delimiter=',')
        for cdr in cdr_buffer:
            wr.writerow(list(cdr))

@click.command()
@click.argument('filename', type=click.Path(exists=True))
@click.option('--version', default=7, help='version of Tetra software 5 or 7')
def main(filename, version):
    # append log files if DEBUG is set (from top of file)
    init_logging('test.log', True)

    global LOG
    LOG = logging.getLogger(__name__)
    LOG.info('Hello world!')

    cdr_buffer: List[Gcdr] = parseCDR(filename, version)
    write_to_csv(cdr_buffer, 'hello.log')

if __name__ == '__main__':
    main()

