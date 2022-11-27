import datetime
from sqlalchemy import Column, Integer, Float, Date, DateTime, String, Boolean
from sqlalchemy import ForeignKey
from sqlalchemy.orm import relationship
from pymix.db_model.BaseType import Base

class Price(Base):
    __tablename__ = 'price'
    id = Column(Integer, primary_key=True)
    ticker = Column(String, index=True)
    bid = Column(Float)
    ask = Column(Float)
    source = Column(String)
    is_active = Column(Boolean, default=True)
    date_added = Column(DateTime, default=datetime.datetime.now)
    last_updated_by = Column(String)

    def as_dict(self):
        return {
            'ticker': self.ticker,
            'bid': self.bid,
            'ask': self.ask,
            'source': self.source,
            'is_active:': self.is_active,
            'date_added': self.date_added.isoformat() if self.date_added is not None else None,
            'last_updated_by': self.last_updated_by
        }

    def __repr__(self):
        date_added = self.date_added.isoformat() if self.date_added is not None else None
        this_repr = f"ticker={self.ticker},is_active={self.is_active},source={self.source},bid={self.bid},ask={self.ask},date_added={date_added}"
        return f"<{self.__tablename__}({this_repr})>)"

    def __eq__(self, other):
        if not isinstance(other, Price):
            return False

        return self.ticker == other.ticker and self.bid == other.bid and self.ask == other.ask and self.source == other.source